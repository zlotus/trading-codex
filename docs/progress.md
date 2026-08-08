# 项目进度

最后审阅：2026-08-09

## 当前里程碑

Milestone 4 进行中。市场状态 contract、四策略池、受约束分配器和 walk-forward
评估器已完成实现与合成验收；由于本地真实日线没有前复权轨且历史长度不足，尚未形成
真实 OOS 绩效证据，不能关闭该里程碑。

## 当前基线

仓库现在包含：

- FastAPI 和 React 应用骨架，以及 framework-independent Python 模块边界。
- 内容寻址的不可变 BaoStock raw cache、固定 schema 的 normalized Parquet、来源和
  接收时间 provenance，以及显式 `as_of` 查询。
- instrument、交易日历、历史 universe、日线、复权因子和 5 分钟数据适配；停牌和
  ST 状态保存在规范化日线中，corporate action 使用独立 schema。
- 默认完全离线的同步命令。只有 `--fetch-missing` 可以回源，且每个进程硬限制最多
  尝试 1 次上游数据请求；失败尝试也消耗预算。
- 数据质量与 09:35 覆盖报告，以及通过 ARM64 验证的 RQAlpha 6.3.0 日频窄适配器。
- ADR-0003 已接受 RQAlpha 作为可替换的回测执行适配器；策略、风险和人工成交仍不
  依赖 RQAlpha。
- 不可变 `DecisionSnapshot` 强制显式 `as_of`，并用 canonical SHA-256 关联输入、
  配置和可重放的决策结果。
- 本地 Parquet 快照源严格配对 BaoStock `adjustflag=2` 前复权信号价与
  `adjustflag=3` 不复权执行价；缺失、future 或不一致数据会 fail closed。
- `DecisionSnapshot` v2 显式携带 `decision_point`、独立状态 universe、日线
  `amount/turnover` 和精确 09:35 五分钟 bar；09:35 决策只读取前一完整交易日的日线，
  风险与下单参考价使用当日 09:35 状态，全部输入受同一 `as_of` 和 provenance 约束。
- 解释型状态管线根据趋势、波动率、宽度、换手、集中度和开盘收益生成 `risk_on`、
  `mean_reverting`、`defensive`、`risk_off` 四种概率及可审计解释。
- 策略池包含波动率缩放动量、短周期反转、防御性低波动和现金；分配器默认只在 09:35
  切换 active strategy，使用 8% 迟滞、20% 单次换手上限和紧急 risk-off 覆盖层，同时
  保留最多 8 个持仓、单票 20% 和 gross exposure 95% 硬边界。
- 前序 `AllocationState` 是 decision hash 的显式输入；`HistoricalReplay` 顺序传递状态，
  ledger 可按明确 `before` 边界恢复最近 M4 base/AI-shadow 分配，不使用隐藏进程状态。
- walk-forward 评估器使用滚动训练窗和不重叠测试窗，统一报告扣费后表现、市场状态切片、
  参数敏感性、最大回撤、alpha/beta、移动 block bootstrap 和 Deflated Sharpe。
- 硬风险和 execution planner 覆盖 stale data、停牌、ST、涨跌停、T+1、整手、
  可卖数量、费用及现金约束；`HistoricalReplay` 直接调用相同 `DecisionPipeline`。
- SQLite 事件账本仅允许追加 decision run、signal、order intent、fill、cash movement、
  signal disposition 和 job attempt；数据库 trigger 拒绝 `UPDATE` 与 `DELETE`。
- ledger schema v2 单独记录 `regime_version` 与 `allocator_version`；v1 迁移只追加 legacy
  标记列，不改写历史 decision payload。
- base、AI-shadow 和 actual 使用同一事件 schema。人工 HTTP 写入只允许 actual track，
  所有人工写操作具备 idempotency key，冲突 payload 会 fail closed。
- 现金与 position lot 按显式 `as_of` 重放；partial fill、费用、T+1、跳过剩余信号和
  缺失估值价格均有确定性处理，信号可追溯至 decision、snapshot 和 source payload。
- EOD preparation 与 09:35 decision 使用稳定 run key 和追加式 attempt event；失败可
  重试，成功 run 不会重复执行。
- Web 已接入决策表、前复权/不复权双价格图、人工成交、跳过信号、三轨权益和持仓
  reconciliation；移动端仅让宽表局部横向滚动。
- BaoStock 前复权日线是显式 opt-in，使用独立 exact-query cache key；默认离线和
  单进程最多一次上游请求的门禁没有放宽。
- `/api/v1/system/status` 保持 `research` mode，历史数据、决策内核、账本和回测边界
  已就绪，实时行情与 AI 仍未配置。

## 进行中

M4 的真实数据评估尚未完成。当前没有运行中的 BaoStock 获取任务；扩样仍必须人工、串行，
遵守每进程最多一次上游请求的门禁。

## 下一步

1. 设计并人工补齐足够长的 `adjustflag=2/3` 日线、历史 universe 和 09:35 状态样本；先
   明确训练、验证、测试日期，不能为了已有结果回填测试区间。
2. 用真实 replay 生成多组版本化参数的 `EvaluationPeriod`，产出并审阅首份扣费 OOS
   报告；若覆盖、成交成本或统计证据不足，继续 fail closed。
3. 报告通过后校准状态阈值、迟滞和换手上限；任何参数变化都提升配置版本并重跑完整
   walk-forward，之后才能关闭 Milestone 4。

## 风险与限制

- 真实 09:35 样本目前只覆盖 2024-06-03 至 2024-06-07 的 19 个标的，共 95 个
  标的日；样本内覆盖为 95/95，不能外推为全市场或全历史覆盖。
- 当前真实样本的 adjustment factor 和 corporate action 规范化表为空。送股账务仅由
  合成 RQAlpha fixture 验证，真实 provider 映射仍需独立样本。
- 当前真实 normalized 日线尚未缓存 `adjustflag=2` 前复权轨，因此不能从该样本构建
  可执行决策快照。补样本必须显式 opt-in，且每次只处理一个 exact-query cache miss。
- 2026-08-08 只读复核显示日线共 97 行、19 个标的且全部为 `adjustflag=3`；五分钟数据
  共 4,656 行。该样本不满足 20 日双价格状态快照，更不满足默认 252/63 walk-forward
  训练/测试窗，当前没有真实绩效结论。
- 新账本默认从零现金开始，必须先通过 actual cash movement API 追加初始资金；目前
  Web 尚未提供现金变动表单。
- 当前没有成交纠错 endpoint。发现错误 fill 时不能修改数据库，必须等待显式补偿事件
  contract。
- daily job 只有可重试执行边界；自动调度、provider health 和告警仍属于 Milestone 6。
- BaoStock 免费 endpoint 存在封 IP 风险。扩展本地样本时必须人工、串行、一次只补
  一个 cache miss，不能使用批量循环或并发回源。
- RQAlpha 当前固定为 6.3.0 并隔离运行。用途变为商业场景或升级版本前，需要重新
  核对源码许可说明和全部 adapter fixture。

## 验证

- 2026-08-09：`.venv/bin/pytest` 通过，57 个测试；M4 新增覆盖精确 09:35 因果边界、
  六类状态特征与概率、四策略池、迟滞、切换时点、换手封顶、紧急 risk-off、前序状态
  恢复、ledger v1→v2 迁移，以及 walk-forward/状态切片/bootstrap/Deflated Sharpe。
- 2026-08-09：`.venv/bin/ruff check .` 通过。
- 2026-08-09：`pnpm --dir web build` 通过，Vite 6.4.3。
- 2026-08-09：`UV_CACHE_DIR=/tmp/trading-codex-uv-cache uv lock --check` 通过，锁文件与
  项目依赖一致。
- 2026-08-09：`git diff --check` 通过。
- 2026-08-08：只读 `trading-codex-data quality` 通过；当前 instruments 8,885 行、
  historical universe 39,557 行、日线 97 行、五分钟 4,656 行，缺少前复权日线、
  adjustment factor 和 corporate action 数据。
- 2026-08-08：Chromium 对带部分成交数据的今日决策和组合对账完成 1440×1000 与真实
  390×844 CSS viewport 检查；全页无横向溢出，决策宽表保留局部滚动。
- 2026-08-08：RQAlpha spike 在 `aarch64`、Python 3.12.3、RQAlpha 6.3.0 下
  通过 T+1、手数、停牌、涨跌停、费用和送股 fixture。
- 2026-08-08：19 标的离线同步重放得到 `cache_hits=19`、`cache_misses=0`、
  `upstream_requests=0`，数据质量报告状态为 `passed`。
- 2026-08-08：2024-06-03 至 2024-06-07 的 09:35 覆盖评估为 95/95，calendar
  和 historical universe 前置数据均完整。
