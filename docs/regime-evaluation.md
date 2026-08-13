# 市场状态与 walk-forward 评估

Milestone 4 的研究 contract 位于共享决策核心，不依赖 RQAlpha 策略 API。实现负责生成
可审计的状态、策略提案、受约束目标组合和统计报告；它不创建成交，也不绕过硬风险检查。

## 决策输入

`DecisionSnapshot` v2 除候选证券外，还要求独立的 `regime_codes`。状态计算使用：

- 截止前一完整交易日的前复权日线计算 20 日趋势、市场宽度和等权市场波动率；
- 同期不复权日线 `turnover` 和 `amount` 计算平均换手率及成交额集中度；
- 决策日精确 09:35 的不复权五分钟 bar 计算成交额加权开盘收益。

`average_turnover` 保留 BaoStock `turn` 的百分数单位，例如 `0.65` 表示约 0.65%，首版
标准化中心和尺度均为 `1.00`。它不与收益类特征使用的十进制比率单位混用。

全部输入必须在快照 `as_of` 时可用。默认要求状态 universe 至少 5 个标的、日线与 09:35
覆盖率均不低于 80%；错误时点或不足覆盖会拒绝整次决策。阈值只是首版研究配置，不能在
没有新版本和 walk-forward 证据时静默修改。

## 状态和策略

| 市场状态 | Active strategy |
| --- | --- |
| `risk_on` | `momentum` |
| `mean_reverting` | `short_term_reversal` |
| `defensive` | `defensive_low_volatility` |
| `risk_off` | `cash` |

默认只在 `opening_0935` 改变 active strategy。新状态相对当前状态的概率优势至少达到
`0.08` 才切换；单次目标组合换手率上限为 `0.20`。紧急 risk-off 可以立即把目标降为现金
并绕过换手上限，但非 09:35 时不改变 active strategy。停牌、跌停、T+1 和现金等执行约束
仍由后续确定性风险与 execution planner 决定。

调用方必须显式传入前序 `AllocationState`。`HistoricalReplay` 会按时间顺序从上一次目标
构造下一次输入；日常 job 应从持久化 decision 轨迹恢复同一 contract，不能依赖 Python
对象的隐式状态。

## 评估报告

`WalkForwardEvaluator` 接收按参数版本分组、具有相同 `as_of` 网格的
`EvaluationPeriod`。每个 period 显式包含 `gross_return`、`benchmark_return`、
`cost_rate` 和当期市场状态。评估器在滚动训练窗内选择参数，只将紧随其后的完整、互不
重叠测试窗汇总为 OOS 结果。

`WalkForwardReport` 包含：

- 扣费后的累计/年化收益、波动率、Sharpe、最大回撤、年化 alpha、beta 和平均成本；
- 每个市场状态的独立 OOS 切片；
- 所有参数在同一 OOS 日期上的敏感性比较；
- 固定 seed 的移动 block bootstrap 主动收益置信区间及为正概率；
- 考虑候选参数数量、偏度和峰度的 Deflated Sharpe 概率。

这些字段是评估能力，不等同于真实策略表现。

## M8.1 EOD 工程 smoke

默认 09:35 contract 保持不变。M8.1 为没有历史分钟线的真实规模工程检查增加独立版本
`interpretable-market-regime-eod-v1`：显式关闭 opening 特征、将 `opening_coverage` 和
`opening_return` 记为 0，并在解释中写入 `opening_feature=disabled_eod`。它不是用零值冒充
09:35 观测，也不能把 EOD 结果外推到 opening 决策。

2026-08-11 的固定成分 smoke 使用 800 个标的、528 个交易日、默认 252/63 walk-forward 和
`turnover_10pct`/`turnover_20pct` 两组参数。报告包含 4 个完整 fold、252 个 OOS observation、
3 个非空 regime slice 和 2 组敏感性。被选择参数的 OOS 累计收益为 -10.7422854322%，最大
回撤 24.3235724898%，Sharpe -0.690697168600，平均成本率 0.000262537861。

这些数值来自固定 2024-06-07 成分、非官方固定成分等权 benchmark，且未应用 corporate action；
artifact 明确设置 `survivorship_bias=true` 和 `formal_m4_oos=false`。它只证明共享决策、成本、
RQAlpha 和统计路径能在真实规模完整运行。M8.2 已补齐 point-in-time universe 和中证800
benchmark；仍须补 corporate action 和经批准范围内的 09:35 数据，并冻结 untouched test 边界，
才能生成 M4 正式报告。
