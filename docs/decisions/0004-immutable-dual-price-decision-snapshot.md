# ADR-0004: 使用不可变双价格快照驱动共享决策管线

- 状态：Accepted
- 日期：2026-08-08
- 取代：无
- 被取代：无
- 后续变更：双价格决策 contract 仍有效；下文原有的“单进程最多一次上游请求”门禁已由
  [ADR-0010](0010-unix-style-raw-baostock-pipeline.md) 的全局串行 raw 下载边界取代。

## 背景

Milestone 2 需要让历史 replay 与后续日常决策使用同一套特征、策略、分配、风险和
执行计划语义。A 股研究还有一个容易混淆的价格边界：复权价格适合计算跨期收益，
但不能作为真实成交、估值、费用或涨跌停判断的价格。若在各模块临时选择价格或直接
查询可变数据，相同决策就难以重放，也容易引入未来数据。

## 决策

1. 每次决策先构建不可变 `DecisionSnapshot`。快照必须声明带时区的 `as_of`、决策日、
   执行失效时间、现金、持仓及可卖数量、交易规则和所有输入 payload 的 SHA-256。
   使用 canonical SHA-256 生成稳定的 `snapshot_id`。
2. 信号价格只读取 BaoStock `adjustflag=2` 前复权日线；估值、目标数量、费用和交易
   约束只读取 `adjustflag=3` 不复权日线。两轨必须按相同 `code/date` 一一配对，任一
   缺失、晚于 `as_of` 或交易状态不一致时整次快照构建失败。
3. feature、strategy intent、target allocation、risk decision 和 execution plan 都是
   带版本的 framework-independent contract。首个策略固定为 volatility-scaled
   cross-sectional momentum，AI 不在该管线中。
4. `HistoricalReplay` 直接调用日常决策使用的 `DecisionPipeline`。RQAlpha 继续位于
   adapter 边界，承担后续历史撮合和账务，不成为策略或风险 API。
5. stale/missing 数据和目标组合硬约束违反会使整次决策 fail closed；停牌、ST 买入、
   涨跌停和 T+1 等单标的约束形成可审计 rejection。执行 planner 再独立检查整手、
   可卖数量、现金和费用，不能产生负现金计划。

## 理由

单一、内容可寻址的输入边界使策略输出和执行计划可以被精确归因。双价格轨避免用
平滑后的研究价格伪造可成交性或账务结果。让 replay 调用同一 pipeline，则因果性和
风险 fixture 能直接约束未来日常决策，而不是维护两套相似实现。

## 考虑过的方案

- 全部使用不复权价格：拒绝，因为 corporate action 会扭曲动量和波动率。
- 全部使用前复权价格：拒绝，因为该价格不能用于成交、费用、现金和涨跌停判断。
- 由每个模块直接查询 Parquet：拒绝，因为查询范围和 `as_of` 容易漂移，且无法形成
  一个可重放的输入标识。
- 在 RQAlpha 策略 API 内实现信号：拒绝，因为这会破坏 ADR-0001 的共享核心边界。

## 后果

- 形成决策前，本地 cache 必须同时具备精确匹配的前复权和不复权日线。前复权需求
  仍需显式 opt-in；实际回源只允许经过 ADR-0010 定义的全局串行 raw 下载边界。
- 当前真实样本尚未验证前复权轨和 corporate action 映射；Milestone 2 的因果性与
  交易约束由合成 fixture 验证，不能表述为实盘数据验证。
- `DecisionSnapshot` 目前是内存 contract；持久化 decision run、订单意图和三条组合
  轨道属于 Milestone 3。
