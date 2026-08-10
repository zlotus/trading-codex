# 实施计划

本计划先验证数据与账务正确性，再增加策略复杂度或扩大 AI 权限。

## Milestone 0：应用骨架

### 交付物

- 仓库内维护的项目上下文、ADR 和面向验收的计划。
- 提供健康检查和组件状态 endpoint 的最小 FastAPI 服务。
- 面向日常决策工作区和 AI 侧边栏的响应式 React 外壳。
- 数据、策略、市场状态、风险、回测、AI 和组合模块的 Python package 边界。

### 验收标准

- 后端测试和 lint 通过。
- 前端生产构建通过。
- API 和 Web 开发服务器可以在本机启动。

## Milestone 1：数据基础与回测可行性验证

### 交付物

- BaoStock 证券、交易日历、日线、复权、停牌、ST 和历史标的池数据采集。
- 不可变 raw 存储、规范化 Parquet 数据集和数据质量报告。
- 针对 09:35 决策点的历史开盘及 5 分钟数据覆盖评估。
- 使用 20 个证券和代表性边界日期完成 RQAlpha 适配器 spike。

### 验收标准

- 增量同步具备幂等性。
- 每条规范化记录都保留来源和接收时间 provenance。
- 时点测试拒绝 future row。
- T+1、手数、费用、停牌、涨跌停和一个 corporate action 场景与独立计算的
  fixture 一致。
- 通过有文档记录的 go/no-go 决策选择 RQAlpha 或窄自定义模拟器。

## Milestone 2：共享决策内核

### 交付物

- 版本化特征管线和日终候选 shortlist。
- 首个确定性策略：波动率缩放的横截面动量策略。
- 目标分配器、硬风险引擎和执行 planner。
- 使用日常决策同一管线的历史 replay。

### 验收标准

- 完整历史和截断历史的因果性检查在抽样时间戳生成相同信号。
- 重放未改变的决策快照时生成相同的基础目标组合。
- 不得基于过期数据或违反 A 股执行约束提出交易。

## Milestone 3：账本与日常垂直切片

### 交付物

- 仅追加的信号、订单意图、成交、现金变动和持仓视图。
- 在同一账本模型上使用基础、AI-shadow 和实际组合标识。
- 带可重试运行记录的日终准备任务和 09:35 决策任务。
- 可用的决策表、信号详情图、成交录入和 reconciliation UI。

### 验收标准

- 部分成交、跳过的信号、费用和 T+1 可用数量能够对账。
- 每个账本事件发生后，账务不变量仍然成立。
- 可以从来源快照一路追踪决策，直到实际成交或失效。

## Milestone 4：市场状态感知策略池

### 交付物

- 根据趋势、波动率、市场宽度、换手率、集中度和开盘时段特征生成可解释的
  市场状态概率。
- 动量、短周期反转、防御性低波动和现金策略。
- 具备迟滞、换手限制和紧急 risk-off 的受约束分配器。
- walk-forward 和按市场状态切片的评估报告。

### 验收标准

- 只允许在已配置的决策点变更策略。
- 每次决策运行都包含市场状态版本和分配器版本。
- 以扣除成本后的结果报告表现，并提供参数敏感性、回撤、alpha/beta、
  block bootstrap 和 Deflated Sharpe 证据。

## Milestone 5：AI 研究与影子分配

### 交付物

- 与 provider 无关的 LLM 客户端，具备结构化输出、prompt 版本、预算、超时、
  缓存和审计记录。
- 使用物理隔离测试数据的离线研究工具。
- 每日 AI 提案仅允许调整已批准的策略权重或降低风险；确定性 fallback 是不变更。
- 结构化 AI 摘要、提案、证据和对话视图。

### 验收标准

- 拒绝无效、迟到、引用未知策略或超出边界的提案。
- AI 没有直接写入成交或风险配置的路径。
- 基础组合和 AI-shadow 结果始终可以独立衡量。

## Milestone 6：前瞻模拟运行

### 交付物

- 定时日常运行、provider 健康检查、告警、备份和 replay 工具。
- 至少 60 个交易日的基础组合和 AI-shadow 观察结果。
- 分离策略、AI 叠加、模拟执行和人工执行影响的复盘报告。

### 验收标准

- 数据或模型中断时必须 fail closed，且不能破坏组合状态。
- 每个观测到的差异都具备可重现的决策和账本轨迹。
- 扩大任何 AI 权限都需要新增一个已接受的 ADR。

## Milestone 7：BaoStock 外挂下载同步 CLI（已完成）

本里程碑只交付独立的 `trading-codex-baostock` 命令行程序。主应用、回测和现有
`trading-codex-data` 保持离线；只有新 CLI 的 `fetch` 子命令可以访问 BaoStock。用户提供的
官方 [blacklist 页面](https://www.baostock.com/blacklist) 给出单 IP 每日 `50,000` 次访问
上限并禁止并发，2026-08-09 已直接在线核实。页面未定义自然日时区、socket 计数或 QPS，
因此项目按每个 socket attempt 保守计数；官方黑名单错误 `10001011` 必须硬停止且禁止自动重试。

完整 M7.0-M7.5 contract、目录、预算、命令和恢复语义见
[`baostock-download-plan.md`](baostock-download-plan.md)，长期安全边界见 ADR-0009；
ADR-0008 已被取代。

### 交付物

- `doctor`、`plan create/show/freeze`、`status`、`fetch`、`sync`、`verify`、`import-raw`
  和 `recover` 子命令；除 `fetch` 外全部从代码结构上零网络。
- 跨进程、跨 manifest、跨 data root 的 global provider lock；从 login 持有到正常 logout，
  或黑名单路径本地关闭 socket 并完成终态落盘，第二个进程在任何 socket 发送前 fail closed。
- 不可上调的 provider `50,000/IP/日` 上限、项目 `45,000` 硬上限、默认 `2,000` 自然日和
  rolling 24-hour 预算、至少 3 秒持久 cooldown，以及 append-before-send attempt ledger。
- frozen exact-query manifest、有界分页和 session item 数；`fetch` 单进程严格顺序下载
  immutable raw，不并发、不自动重试、不动态扩展查询范围。
- 由唯一 `--data-root` 直接指定任意落盘目录；在 login 前检查容量、write、`fsync`、atomic
  replace 和 `flock`，不识别系统盘/数据盘、mount point 或 UUID。默认目录适配 300 秒
  `hd-idle` 的 pre-wake 和短会话；raw 到 normalized 的离线 staging/immutable segment、
  quarantine 和断点恢复。
- 同一 data root 的 fetch/离线发布/恢复排他锁，以及 content-addressed verify report 和不可覆盖
  completion receipt。
- 当前必要 BaoStock endpoint 的固定字段 contract、fake socket fixture、稳定 JSON 状态和
  操作文档；现有 `sync --fetch-missing` 联网入口移除或永久禁用。

### 验收标准

- provider/page/login/logout/失败 attempt 全部计数；失败和进程崩溃不返还预算，重启、午夜或
  修改 data root 不能绕过 lock、cooldown、自然日和 rolling 24-hour 门禁。
- 两个并发 CLI 进程只有一个能在 fake socket 上发送；不存在 `--workers`、后台 daemon、timer
  或其他网络入口。同公网 IP 下另有 BaoStock 客户端时，操作规程要求停止本 CLI。
- `10001011` 会持久化为 `provider_blacklisted` 硬停止状态并禁止继续发送 logout；跨日、
  cooldown 到期或更换 manifest 均不能自动恢复，必须按官方说明联系管理员，并在确认解除后
  通过不重置预算的追加式人工恢复事件显式开放新会话。
- cache hit 产生零网络；draft、hash 不匹配、超预算、超页、低空间、state 损坏、schema drift、
  raw fsync 或 normalized 冲突全部 fail closed。
- `plan/status/sync/verify/doctor` 的单元测试证明无法登录；离线重复同步 deterministic，segment
  中断不破坏已发布数据。
- 完整测试和 lint 通过后，最多执行一个由用户明确批准的 `--max-items 1` schema pilot，并保存
  全部 socket attempt 与 raw hash。未执行 pilot 时只能标记 `live_pilot_pending`。
- M7 完成只表示 CLI 可安全使用，不表示历史数据已补齐、M4 已关闭或 M6 可启动。

M7.0-M7.5 已完成。2026-08-10 在 `/mnt/exos_1t/quant/baostock` 执行了 30 秒间隔、
`--max-items 1` 的真实 pilot：4 次 socket attempt 全部成功，3,644 行前复权日线完成 immutable
raw、normalized segment、逐行 verify 和 completion receipt。异卷 backup target 仍是任何 M8
bulk wave 的前置门禁，不属于 M7 单项 pilot 的完成声明。

## 拟议 Milestone 8：真实回填与 OOS 验收

使用 M7 CLI 执行预先冻结的日期、universe、train/validation/test manifest，依次补齐批量
日线与因子、历史指数成分、provider `adjustflag=2/3` 双价格、09:35 五分钟数据和 corporate
action；随后完全离线运行质量门禁、RQAlpha 对账和 walk-forward，生成 immutable OOS bundle。

API 优先级、数据 schema、M8.0-M8.5 切片和完成边界见
[`baostock-data-plan.md`](baostock-data-plan.md)。M8 报告经人工审阅前，M4 保持未完成，M6
timer、forward observation 和 live AI proposal 均不得启动。

## 延后事项

- Qlib 机器学习因子模型。
- 为时点 AI 上下文采集新闻和公告。
- 付费市场数据 provider 和自动化券商 gateway。
- 决策周期短于 5 分钟的日内策略。
