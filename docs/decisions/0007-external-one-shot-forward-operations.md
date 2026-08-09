# ADR-0007：使用外部一次性调度和 append-only 前瞻运维证据

- 状态：Accepted
- 日期：2026-08-09
- 取代：无
- 被取代：无

## 背景

Milestone 6 需要定时日常运行、provider 健康检查、告警、备份、replay 和至少 60 个
交易日的前瞻归因。FastAPI 可能以 reload 或多个 worker 启动；若在应用 lifespan 内启动
后台 scheduler，同一个时点可能重复执行。进程崩溃也可能把 job 永久留在 `running`。

系统仍是本地单用户模块化单体，组合事实保存在 append-only SQLite 账本中。M4 的真实
OOS 证据、实时行情 adapter 和模型 adapter 尚未就绪，因此当前只能先建立不会伪造前瞻
结果、也不会越过 provider 门禁的运维 contract。

## 决策

1. 使用外部 timer 负责唤醒和进程监督，应用只提供一次性 `OneShotDailyScheduler`。
   scheduler 只考虑当前 Asia/Shanghai 交易日的 09:35 与 15:30 窄时间窗，不自动回补
   错过的运行，也不嵌入 FastAPI lifespan。
2. 每个 schedule 使用稳定 job run key。开始 attempt 必须在 SQLite `BEGIN IMMEDIATE`
   事务内获取 30 分钟 lease；并发 attempt 会观察现有 `running` 状态而不执行任务。lease
   超时后先为旧 attempt 追加 `failed` 事件，才能开始新 attempt；成功 run 保持终态幂等。
3. daily task 前运行可注入的 provider probes。所有 critical probe 只有 `healthy` 才能
   通过；`degraded`、`unavailable` 或 critical `not_configured` 都会先记录 health/alert，
   再阻断任务函数。optional provider 的 `not_configured` 不阻断 base 轨，也不授权 AI。
4. ledger schema v4 追加 `provider_health_checks`、`alert_events` 和
   `forward_observations`。这些表与既有事件表一样由 trigger 拒绝 `UPDATE` 和 `DELETE`；
   API 只公开状态与观察记录的只读查询。
5. 使用 SQLite online backup 生成 WAL 一致数据库副本，并写入不可变 SHA-256 manifest。
   verify 同时检查文件 hash、逻辑内容、`quick_check`、foreign key 和 append-only trigger；
   replay 只在临时副本上迁移和重建三轨组合，不修改备份。
6. 每个前瞻 observation 必须关联同一 snapshot 的 base 与 AI-shadow decision、两组配置、
   market-data payload hashes 和独立 metric payload hash。归因报告分别计算策略相对 benchmark、
   模拟执行相对无摩擦 base target、AI overlay 相对 base simulation，以及人工执行相对 base
   simulation 的差异；少于 60 个唯一交易日时 fail closed。
7. 本决策不扩大 AI 权限。scheduler、health 和恢复工具均不能创建 actual fill、修改风险
   配置或把未批准策略投入运行。

## 理由

外部 timer 避免 Web worker 数量影响交易任务次数；一次性进程也更容易用 systemd 的退出码、
日志和重启策略监督。append-only lease 保留崩溃与重试历史，同时解决并发重复执行和永久
`running`。把 health、alert、observation 与 decision/ledger 轨迹放在同一 SQLite 事务边界，
可以在本地单用户约束下保持恢复简单且可审计。

内容寻址备份证明“备份了什么”，临时 replay 证明“事件能否重新投影”，两者职责不同。
60 日硬门槛则防止合成 fixture 或不足窗口被展示成真实前瞻证据。

## 考虑过的方案

- 在 FastAPI lifespan 中运行 APScheduler：拒绝，因为 reload、多 worker 和 API 重启会改变
  scheduler 实例数量与任务生命周期。
- 只依赖 cron 防重：拒绝，因为重叠执行、崩溃 lease 和业务幂等仍需要应用内证据。
- 原地更新 job 或 alert 状态：拒绝，因为会丢失故障、恢复和重试顺序。
- 用文件复制直接备份 WAL 数据库：拒绝，因为复制主文件时可能漏掉尚未 checkpoint 的 WAL。
- 不足 60 日时先输出带免责声明的绩效报告：拒绝，因为它仍容易被误读为已完成前瞻验收。
- provider 中断时继续沿用上次决策：拒绝，因为这会突破显式 `as_of` 与 stale-data 门禁。

## 后果

- 真正启用前必须提供交易日历、EOD/09:35 task composition、critical provider probes 和外部
  timer unit；当前代码 contract 完成不等于 scheduler 已上线。
- 外部 timer 调用频率必须覆盖 20 分钟默认窗口；错过窗口需要人工审计后显式处理，不能自动
  追赶并生成过时决策。
- 第一次用 v4 代码打开旧账本前应先创建离线副本；schema 迁移只追加表、列和 trigger。
- notification adapter 尚未配置时，alert 仍可从 ledger API、CLI 非零退出码和进程日志读取，
  但不能声称已有远程通知送达证据。
- M4 未关闭、daily tasks 未接线、provider adapters 未配置以及真实观察不足 60 日时，系统状态
  必须继续显示 `not_configured`，不能进入前瞻运行。
