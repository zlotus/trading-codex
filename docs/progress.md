# 项目进度

最后审阅：2026-08-08

## 当前里程碑

Milestone 2 已完成。版本化特征、首个确定性策略、目标分配、硬风险、执行计划和
同管线历史 replay 均达到实施计划中的验收线，下一步进入 Milestone 3 的账本与
日常垂直切片。

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
- 首个版本化 feature/strategy pipeline 实现 volatility-scaled cross-sectional
  momentum、正动量 shortlist、inverse-volatility 分配、最多 8 个持仓、单票 20%
  和 gross exposure 95% 默认上限。
- 硬风险和 execution planner 覆盖 stale data、停牌、ST、涨跌停、T+1、整手、
  可卖数量、费用及现金约束；`HistoricalReplay` 直接调用相同 `DecisionPipeline`。
- BaoStock 前复权日线是显式 opt-in，使用独立 exact-query cache key；默认离线和
  单进程最多一次上游请求的门禁没有放宽。
- `/api/v1/system/status` 已进入 `research` mode，区分已就绪的历史数据、决策内核、
  回测边界与尚未接入的实时行情、AI。

## 进行中

无。Milestone 3 尚未开始，也没有运行中的 BaoStock 获取任务。

## 下一步

1. 定义 append-only signals、order intents、fills、cash movements 和 position
   views，并保持 base、AI-shadow、actual 三条 portfolio track 可归因。
2. 建立 EOD preparation 与 09:35 decision job 的可重试 run record。
3. 接入决策表、人工 fill 录入和 reconciliation UI，验证 partial fill、费用及 T+1
   可卖数量的账务不变量。

## 风险与限制

- 真实 09:35 样本目前只覆盖 2024-06-03 至 2024-06-07 的 19 个标的，共 95 个
  标的日；样本内覆盖为 95/95，不能外推为全市场或全历史覆盖。
- 当前真实样本的 adjustment factor 和 corporate action 规范化表为空。送股账务仅由
  合成 RQAlpha fixture 验证，真实 provider 映射仍需独立样本。
- 当前真实 normalized 日线尚未缓存 `adjustflag=2` 前复权轨，因此不能从该样本构建
  可执行决策快照。补样本必须显式 opt-in，且每次只处理一个 exact-query cache miss。
- BaoStock 免费 endpoint 存在封 IP 风险。扩展本地样本时必须人工、串行、一次只补
  一个 cache miss，不能使用批量循环或并发回源。
- RQAlpha 当前固定为 6.3.0 并隔离运行。用途变为商业场景或升级版本前，需要重新
  核对源码许可说明和全部 adapter fixture。

## 验证

- 2026-08-08：`.venv/bin/pytest` 通过，36 个测试；包含多个 `as_of` 的完整/截断历史
  causality、同快照 replay、双价格 fail-closed 和 A 股执行约束 fixture。
- 2026-08-08：`.venv/bin/ruff check .` 通过。
- 2026-08-08：`pnpm --dir web build` 通过，Vite 6.4.3。
- 2026-08-08：`uv lock --check` 通过，锁文件与项目依赖一致。
- 2026-08-08：RQAlpha spike 在 `aarch64`、Python 3.12.3、RQAlpha 6.3.0 下
  通过 T+1、手数、停牌、涨跌停、费用和送股 fixture。
- 2026-08-08：19 标的离线同步重放得到 `cache_hits=19`、`cache_misses=0`、
  `upstream_requests=0`，数据质量报告状态为 `passed`。
- 2026-08-08：2024-06-03 至 2024-06-07 的 09:35 覆盖评估为 95/95，calendar
  和 historical universe 前置数据均完整。
