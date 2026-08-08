# ADR-0005：使用 append-only SQLite 事件账本和派生组合视图

- 状态：Accepted
- 日期：2026-08-08
- 取代：无
- 被取代：无

## 背景

Milestone 3 需要把确定性决策、人工成交和三条组合轨道连接成可审计的日常工作流。
事务状态必须能在本地单用户环境中可靠重放，同时保留部分成交、费用、T+1 可卖数量、
跳过信号和任务重试的完整历史。直接维护可变的现金及持仓行会隐藏事件顺序，也会让历史
对账依赖当前状态。

## 决策

1. 使用标准库 `sqlite3` 保存本地事务状态。decision run、signal、order intent、fill、
   cash movement、signal disposition、job run 和 job attempt 都是 append-only 记录；
   SQLite trigger 拒绝对这些表执行 `UPDATE` 或 `DELETE`。
2. base、AI-shadow 和 actual 使用同一个事件 schema，并以显式 `portfolio_track` 区分。
   HTTP 成交、现金变动和跳过接口只允许人工 actual track；base 和 AI-shadow 事件只能由
   应用内部流程生成。
3. fill 与其 trade/fee cash movement 在一个事务中追加。每次影响账务的写入后，从事件
   序列重建现金和 position lot，并检查任一时点不得出现负现金、负持仓或违反 T+1 的卖出。
4. dashboard、信号状态和持仓视图都接受显式 `as_of`，只投影不晚于该时点的事件。缺少
   可用估值价格时，权益返回未知值，不能用零或未来价格替代。
5. 外部可重试操作使用稳定的 job run key 和追加式 started/succeeded/failed attempt event。
   相同 schedule 已成功时直接返回既有结果；失败重试保留每次 attempt。
6. 所有人工写入要求 idempotency key。相同 key 和相同 payload 重放为同一事件；相同 key
   携带不同 payload 时 fail closed。

## 理由

该模型让现金与持仓成为可重建的事实投影，而不是另一套需要同步维护的真相。SQLite 适合
本地单用户模块化单体，事务、外键和 trigger 足以保护当前写入边界，也避免引入独立数据库
服务。显式三轨标识和来源 ID 保留了策略、AI 叠加与人工执行之间的归因。

## 考虑过的方案

- 直接更新 cash 和 position 表：拒绝，因为历史状态、部分成交顺序和人工纠错无法完整重放。
- 每条组合轨道使用独立表：拒绝，因为 schema 漂移会破坏三轨可比性。
- 立即引入 PostgreSQL 或事件流服务：拒绝，因为本地单用户工作负载没有足以承担额外运行
  复杂度的并发需求。
- 允许 AI workflow 直接调用成交接口：拒绝，因为这违反 ADR-0002 的权限边界。

## 后果

- 账本数据库是本地运行状态，不进入 Git；默认路径可通过
  `TRADING_CODEX_LEDGER_PATH` 配置。
- projection 成本随事件数量增长。超过个人日常工作负载前不做缓存；将来若增加 snapshot，
  snapshot 必须可由原始事件验证。
- 当前没有成交纠错 endpoint。后续纠错必须新增显式补偿事件，不能放宽 append-only trigger
  或原地修改 fill。
- job runner 只提供可重试执行边界；定时调度、provider 健康检查和告警仍属于 Milestone 6。
