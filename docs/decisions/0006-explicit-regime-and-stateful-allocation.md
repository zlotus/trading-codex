# ADR-0006：使用显式市场状态输入和可重放的受约束分配

- 状态：Accepted
- 日期：2026-08-08
- 取代：无
- 被取代：无

## 背景

Milestone 4 需要根据趋势、波动率、市场宽度、换手率、成交集中度和 09:35 开盘特征，
在多个预先批准的策略之间进行可解释选择。迟滞与换手限制依赖前一次分配；若把这部分
状态藏在长驻进程内存中，同一快照就可能因调用顺序不同而产生不同结果。市场状态所需的
数据也不能从候选 shortlist 或晚于决策时点的行情中推测。

## 决策

1. `DecisionSnapshot` v2 显式声明 `decision_point`、独立的 `regime_codes`、日线
   `amount/turnover` 和决策日 09:35 五分钟 bar。09:35 决策的日线严格截止前一完整
   交易日，风险与 execution planner 使用当日 09:35 可见价格；Parquet 源对全部查询使用
   同一个带时区 `as_of`，并把相关 payload SHA-256 纳入快照。覆盖率不足、时点错误或
   字段缺失时 fail closed。
2. 使用版本化、确定性的解释型打分器生成 `risk_on`、`mean_reverting`、`defensive` 和
   `risk_off` 四种概率。每次评估保存六类输入特征、各状态 score/probability、选中状态和
   解释；阈值或权重变化必须产生新版本。
3. 四个已批准策略分别为波动率缩放动量、短周期反转、防御性低波动和现金。状态映射到
   一个 active strategy；默认只允许在 `opening_0935` 改变 active strategy，并要求新状态
   概率优势达到 8% 迟滞阈值。
4. 分配器默认把单次目标组合换手率限制为 20%。前序 `AllocationState` 是显式输入，并被
   纳入 decision hash；`HistoricalReplay` 逐次传递同一 contract，不使用隐藏可变状态。
5. 紧急 risk-off 是确定性风险覆盖层。它可在任一已运行的决策点立即把目标降至现金并
   绕过换手上限，但在非配置切换点保留 active strategy；到配置切换点才正式切换为现金。
   实际卖出仍受停牌、跌停和 T+1 等硬风险约束，不能伪造可成交性。
6. walk-forward 使用滚动训练窗选择参数，随后只汇总不重叠测试窗。报告统一使用扣除
   `cost_rate` 后的收益，并包含参数敏感性、最大回撤、alpha/beta、移动 block bootstrap
   和 Deflated Sharpe。统计 contract 与 RQAlpha 适配器解耦。
7. append-only ledger schema v2 单独保存 `regime_version` 和 `allocator_version`。v1 row
   只追加带默认 legacy 标记的新列，不改写既有事件 payload。

## 理由

显式状态 universe 和前序分配输入让日常决策与历史 replay 共享同一条可重放路径。解释型
概率比直接使用不可审计的聚类标签更适合当前样本量，也便于识别每个特征对 risk-on/off 的
影响。把紧急降风险定义为覆盖层，可以同时满足及时减仓和策略只在配置时点切换两项约束。

## 考虑过的方案

- 从候选 shortlist 估计全市场状态：拒绝，因为 shortlist 已被策略筛选，会产生选择偏差。
- 在 pipeline 实例中保存上次状态：拒绝，因为进程重启、并发 replay 和调用顺序会改变结果。
- 每个状态直接连续混合四个策略权重：暂不采用，因为当前真实样本不足以证明额外自由度，
  且会削弱迟滞和策略归因的清晰度。
- 紧急状态在任意时点直接改写 active strategy：拒绝，因为这违反策略只在配置决策点切换的
  验收约束；紧急降风险应作为独立覆盖层。
- 用普通随机重采样替代 block bootstrap：拒绝，因为日收益存在序列相关性。

## 后果

- 日常 decision task 必须从账本或显式调用参数恢复前序 `AllocationState`；缺失时只能视为
  首次初始化，不能猜测上一次策略。
- 状态阈值、迟滞和换手上限都是版本化研究参数，真实采用前必须通过物理隔离的 walk-forward
  与敏感性报告验证。
- 当前本地数据没有 `adjustflag=2` 日线轨，也没有足够长的历史，现阶段只能完成 contract
  与合成因果性验收，不能声称已有真实绩效证据。
