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

## Milestone 7：BaoStock raw 下载工具链（已完成）

本里程碑交付 ADR-0010 定义的 Unix 风格边界：`trading-codex-requirements` 生成 JSONL exact
request，`trading-codex-baostock` 只顺序下载 raw envelope，`trading-codex-data` 独立检查和
预处理。主应用、回测和 scheduler 保持离线。

### 交付物

- 跨 data root 的全局非阻塞 `flock`；一个 login session 内严格顺序，无并发或自动重试。
- 每个底层 socket send 在发送前写入按上海自然日分文件的文本计数；官方上限 50,000，默认
  40,000 次停止。
- exact request 决定 raw 目标路径；文件存在直接跳过，重新运行同一命令就是断点续传。
- versioned canonical JSON envelope、payload/envelope SHA-256、临时文件、`fsync`、atomic
  replace 和下载端落盘前后自检。
- 独立 `inspect-raw` 和 `ingest-raw` 再次验证 envelope，并按 payload hash 幂等发布 normalized
  segment；跨 payload 业务键冲突拒绝发布，坏 raw 只报告 warning，不触发网络请求。
- 下载器不再包含空间预测、mount/UUID 识别、backup 门禁、manifest 状态机或 normalized 验收。
- 旧 `trading-codex-data --fetch-missing` 永久阻断网络；已有 M7 pilot raw/segment 保持可读。

### 验收标准

- 两个进程只有一个能发送；目标文件存在时为零网络。
- 首错停止且无自动重试；中断后重跑只补缺失目标。
- 达到日边界返回暂停，次日继续；`10001011` 写 marker 并硬停止。
- 下载端和预处理端各自能发现 envelope 损坏，不能传递信任。
- 下载进程只创建 raw；normalized、quality、回测和备份由独立工具完成。
- M7 验收本身不证明基础数据回填、M4 OOS 或 M6 启用条件已经完成。

2026-08-10 的旧 manifest pilot 已实际取得单证券双价格各 3,644 行，以及沪深300/中证500共
800 个成分；它们是兼容数据和 schema 证据。简化入口的验证目前使用 fake provider，不新增
BaoStock 请求。

## Milestone 8：真实回填与 OOS 验收

M8.0 已于 2026-08-11 完成：固定 2024-06-07 成分的 1,602 条基础请求已补齐 800 个股票从
2011-01-01 至 2026-08-10 的双价格日线，并完成离线 inspect、干净 ingest 和完整性验收。
下一步 M8.1 运行带幸存者偏差标记的真实规模 smoke；随后另行补 point-in-time universe、
benchmark、corporate action 和必要的 09:35 数据，生成正式 untouched OOS artifact。

API 优先级、M8.0-M8.4 切片和完成边界见
[`baostock-data-plan.md`](baostock-data-plan.md)。固定当前成分的 smoke 有幸存者偏差，不能关闭
M4；M4 正式报告经人工审阅前，M6 timer、forward observation 和 live AI proposal 均不得启动。

## 延后事项

- Qlib 机器学习因子模型。
- 为时点 AI 上下文采集新闻和公告。
- 付费市场数据 provider 和自动化券商 gateway。
- 决策周期短于 5 分钟的日内策略。
