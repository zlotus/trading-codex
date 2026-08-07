# 项目进度

最后审阅：2026-08-08

## 当前里程碑

Milestone 1 已完成。数据基础、09:35 历史覆盖评估和 RQAlpha 账务可行性 spike
均达到实施计划中的验收线，下一步进入 Milestone 2 的共享决策内核。

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

## 进行中

无。Milestone 2 尚未开始，也没有运行中的 BaoStock 获取任务。

## 下一步

1. 定义版本化的 feature、strategy intent、allocation 和 risk domain contract。
2. 建立完整历史与截断历史的 causality test，确保相同 `as_of` 下信号一致。
3. 实现首个确定性策略：volatility-scaled cross-sectional momentum，并通过同一
   contract 接入历史 replay。

## 风险与限制

- 真实 09:35 样本目前只覆盖 2024-06-03 至 2024-06-07 的 19 个标的，共 95 个
  标的日；样本内覆盖为 95/95，不能外推为全市场或全历史覆盖。
- 当前真实样本的 adjustment factor 和 corporate action 规范化表为空。送股账务仅由
  合成 RQAlpha fixture 验证，真实 provider 映射仍需独立样本。
- BaoStock 免费 endpoint 存在封 IP 风险。扩展本地样本时必须人工、串行、一次只补
  一个 cache miss，不能使用批量循环或并发回源。
- RQAlpha 当前固定为 6.3.0 并隔离运行。用途变为商业场景或升级版本前，需要重新
  核对源码许可说明和全部 adapter fixture。

## 验证

- 2026-08-08：`.venv/bin/pytest` 通过，19 个测试。
- 2026-08-08：`.venv/bin/ruff check .` 通过。
- 2026-08-08：`pnpm --dir web build` 通过，Vite 6.4.3。
- 2026-08-08：RQAlpha spike 在 `aarch64`、Python 3.12.3、RQAlpha 6.3.0 下
  通过 T+1、手数、停牌、涨跌停、费用和送股 fixture。
- 2026-08-08：19 标的离线同步重放得到 `cache_hits=19`、`cache_misses=0`、
  `upstream_requests=0`，数据质量报告状态为 `passed`。
- 2026-08-08：2024-06-03 至 2024-06-07 的 09:35 覆盖评估为 95/95，calendar
  和 historical universe 前置数据均完整。
