# BaoStock 数据接入与 Milestone 8 计划

- 状态：Proposed
- 计划版本：`baostock-data-plan-v2`
- 依据版本：BaoStock `0.9.3`

本计划定义独立下载 CLI 就绪后的 BaoStock API 优先级、规范化数据职责和真实回填顺序。目标是
使用 Milestone 7 已实现的 `trading-codex-baostock`，以较少的
上游请求补齐真实双价格、历史指数成分、09:35 状态和 corporate action 数据，生成可重放的
真实 OOS 证据。M7 endpoint contract 和 normalizer 已完成代码验收，且
`query_history_k_data_plus` 日线 `adjustflag=2` 已通过单项真实 pilot；这不代表其他 endpoint
schema 已确认、完整数据已下载或已经授权执行 bulk backfill。

Milestone 7 的 CLI、data root、官方访问上限、manifest 状态机、严格串行 fetch、停止条件和
备份职责见
[`baostock-download-plan.md`](baostock-download-plan.md)。目标数据根目录为
`/mnt/exos_1t/quant/baostock`；M7 单项 live pilot 和离线复验已通过，但异卷备份和 M8.0
边界冻结前仍不得启动系统性回填。

Milestone 8 执行冻结 manifest、补齐 Milestone 4 真实证据并为 Milestone 6 准备数据，不新增
策略、不接券商、不扩大 AI 权限，也不把 BaoStock 当作实时行情 provider。

## 已验证边界

- `uv.lock` 当前锁定 BaoStock `0.9.3`。官方客户端公开了
  `query_daily_history_k_AStock`、`query_daily_adjust_factor`、`query_hs300_stocks` 和
  `query_zz500_stocks` 等接口。
- 2026-08-09 已直接在线核实 BaoStock 官方
  [blacklist 页面](https://www.baostock.com/blacklist)：每日 API 请求不能超过 `50,000` 次，
  禁止并发连接，超出后进入黑名单。黑名单响应为 `10001011`，需在 QQ 群联系管理员并提供
  公网 IP。页面未定义计数细节、自然日时区或 QPS；M7 CLI 按每个 socket attempt 保守计数，
  采用更低的项目预算、跨进程全局锁和持久 cooldown，并把 `10001011` 作为禁止自动重试的
  硬停止错误。
- BaoStock 客户端使用持久 TCP socket。一次高层 API 调用可能包含 login、首个 query、
  `ResultData.next()` 触发的分页 query 和 logout；当前 `CachedBaoStockClient` 的
  `upstream_requests` 只统计 loader 调用，不能代表真实 socket 请求数。
- BaoStock 默认页大小为 2,000 行。五分钟数据约 48 行/交易日，跨越约 40 个交易日的单证券
  query 就可能分页。新批量单日接口由客户端标记为单页最多 20,000 行，但仍需真实单日样本
  验证字段和服务端行为。
- BaoStock 公开 Python API 不提供可依赖的实时行情能力；历史 5 分钟数据不能替代 M6 所需的
  live 09:35 provider。

## 数据处理原则

```text
人工批准的 exact query
  -> immutable raw JSON
  -> 固定 schema normalizer
  -> normalized Parquet
  -> quality / coverage / as_of gate
  -> DecisionSnapshot / RQAlpha / HistoricalReplay
  -> immutable backtest artifact
```

1. `sync` 默认完全离线；cache miss 不得隐式登录或请求 BaoStock。
2. 每个 raw artifact 使用内容 SHA-256 寻址；exact query index 必须包含 endpoint、完整参数和
   provider client version，避免不同接口或版本相互冒充 cache hit。
3. 每条规范化记录保存 `available_at`、`source_received_at`、`source_payload_sha256` 和
   `raw_artifact`。事件型数据的 `available_at` 使用真实公告/可见时间，不能直接等于报告期末。
4. 回测、特征、决策与 API 请求分离。研究运行只能读取本地冻结数据，不得边回测边补数。
5. 任何缺失、future、重复、schema 漂移、双价格不一致或覆盖不足都 fail closed。
6. 训练、验证、测试日期必须在补数前写入 immutable manifest；不能根据结果回填测试区间。

## API 优先级与消费路径

### P0：Milestone 7 CLI 前置门禁

P0 不新增 provider 数据，已由 Milestone 7 代码交付并先于所有真实回填：

- 提供跨进程、跨 data root 的 global provider lock 和持久化 cooldown；进程重启不能绕过。
- 记录 login、query、每个分页和 logout 的真实 socket 请求及结果。
- 同时执行 `50,000/IP/日` provider 硬上限、项目预算、rolling 24-hour、session 和 item 预算。
- 提供零网络 `plan`，列出 exact-query cache hit/miss、预计分页、空间和请求预算。
- `fetch` 只在一个 login session 内有界、严格顺序处理 frozen item；不提供并发入口或自动重试。
- 对可能超过单页的查询按可证明的行数上限切片，不能依赖 `next()` 静默扩大请求数。

### P1：关闭 M4 所需的行情和 universe

| API | 当前状态 | 规范化数据 | 程序用途 |
| --- | --- | --- | --- |
| `query_daily_history_k_AStock(date)` | adapter/normalizer 已实现；live pilot 待确认 | `daily_bars` segments | 批量补不复权日线、成交额、换手、停牌和 ST；用于执行、估值、市场宽度与成交集中度 |
| `query_daily_adjust_factor(date)` | adapter/normalizer 已实现；live pilot 待确认 | `adjustment_factors` segments | 批量保存复权因子，检查 corporate action 和双价格一致性 |
| `query_hs300_stocks(date)` | adapter/normalizer 已实现；live pilot 待确认 | `index_memberships` segments | 构建时点沪深 300 成分，参与候选和独立状态 universe |
| `query_zz500_stocks(date)` | adapter/normalizer 已实现；live pilot 待确认 | `index_memberships` segments | 构建时点中证 500 成分，并与沪深 300 合并为目标 universe |
| `query_history_k_data_plus` 日线 | `adjustflag=2` 单项 live pilot 已通过；完整回填待 M8 | `daily_bars.parquet` | 获取 `adjustflag=2` 信号轨；必要时有界补 `adjustflag=3` 并获取指数 benchmark |
| `query_history_k_data_plus` 5 分钟 | 已接入 31 日切片与分页门禁；live pilot 待确认 | `five_minute_bars.parquet` | 只为已冻结的状态/候选代码提取精确 09:35 bar |
| `query_trade_dates` | 已接入 | `trade_calendar.parquet` | 交易日、调度边界和 walk-forward 日期网格 |
| `query_stock_basic` | 已接入 | `instruments.parquet` | 证券类型、上市/退市日期和代码有效性 |
| `query_all_stock(day)` | 已接入 | `historical_universe.parquet` | 保存某日上市及交易状态；不得替代沪深 300+500 策略 universe |
| `query_adjust_factor` | 已接入 | `adjustment_factors.parquet` | 单证券补缺，并与批量复权因子交叉验证 |

新增 `index_memberships.parquet` 至少包含：

- `snapshot_date`、`index_code`、`member_code`、`member_name`；
- 标准 provenance 字段；
- 主键 `snapshot_date + index_code + member_code`。

benchmark 行情建议使用独立 `benchmark_bars.parquet`，避免指数缺少股票专属
`tradestatus/isST` 字段时放宽 `daily_bars` 的严格 schema。它至少保存交易日、指数代码、
OHLC、成交量/额、`available_at` 和 provenance，用于 `EvaluationPeriod.benchmark_return`、
alpha/beta 及前瞻归因。

`query_daily_history_k_AStock` 只有 `date` 参数，不能选择 `adjustflag`。首个真实样本必须确认
其返回轨道；在确认前不得把它写成 `adjustflag=3`。即使批量不复权日线与批量因子齐全，当前
仍必须从 BaoStock `adjustflag=2` 获取信号轨。若要改为本地派生，必须新增 ADR 取代或修订
ADR-0004，并用跨 corporate action 样本证明结果完全一致。

### P2：交易真实性

| API | 当前状态 | 规范化数据 | 程序用途 |
| --- | --- | --- | --- |
| `query_dividend_data` | adapter/normalizer 已实现；真实映射待 M8.4 | `corporate_actions` segments | 分红、送股、转增、登记日、除权日和派息日；供 RQAlpha dividend/split 与实际账务验证 |
| `query_stock_industry` | 待接入 | `industry_memberships.parquet` | 行业暴露和 OOS 切片；首版只报告，不直接新增风险限制 |
| `query_daily_history_k_ETF` | 延后 | 独立 ETF 日线数据集 | 后续现金替代品、ETF benchmark 或明确批准的 ETF 策略 |

`industry_memberships.parquet` 必须保存分类日期和 provenance。行业数据进入硬风险或目标分配前，
需要单独接受配置和边界；Milestone 8 只允许将其用于研究报告。

BaoStock dividend 字段只有公告日期，没有日内发布时间。当前 normalizer 为避免当日信息泄漏，
把 `available_at` 保守设置为公告日后一个自然日的 00:00（`Asia/Shanghai`）；M8.4 若要使用更
精确的公告时间，必须引入可审计的独立来源和新 contract。

### P3：暂缓的研究接口

- `query_profit_data`、`query_operation_data`、`query_growth_data`、`query_dupont_data`、
  `query_balance_data`、`query_cash_flow_data`；
- `query_performance_express_report`、`query_forecast_report`；
- 存贷款利率、存款准备金率和货币供应量接口。

这些接口只有在公告时间、修订历史、缺失值策略和 point-in-time contract 明确后才能进入离线
研究。它们不帮助关闭当前 M4 数据缺口，因此不属于 Milestone 8 必交付物。

## Milestone 8 实施切片

### M8.0：冻结回填范围与 schema pilot

交付：冻结历史日期、预热、train/validation/test、point-in-time universe 和 benchmark；使用
M7 CLI 为每个新增 endpoint 生成单项 pilot manifest，并人工审阅字段、页数、socket 计数和
实际落盘大小。

完成条件：每个 pilot 的 raw hash 可离线重放；API 日期、字段和 `adjustflag` 语义已经确认；
provider 预算和空间估算通过。未知或漂移字段进入 quarantine，不能继续批量回填。

### M8.1：批量单日日线与复权因子

交付：通过 `query_daily_history_k_AStock`、`query_daily_adjust_factor` 的 frozen manifest
补齐不复权日线和复权因子，并从本地 raw 同步固定 schema Parquet。

完成条件：日期、代码和业务主键唯一；离线重放得到相同 Parquet 和 hash；未知
`adjustflag`、重复代码、future 或字段漂移 fail closed。

### M8.2：历史目标 universe 与 benchmark

交付：沪深 300/中证 500 历史成分、`index_memberships.parquet`、benchmark 行情与覆盖报告。

完成条件：历史决策只看当时成分；候选、状态和全市场交易状态职责分离；benchmark 与
`EvaluationPeriod` 使用同一日期网格，缺口阻止 OOS 报告。

### M8.3：有界双价格与 09:35 backfill

交付：根据冻结 manifest 对 `adjustflag=2/3` 和五分钟数据做 page-aware 串行回填；生成
双价格、状态 universe 和 09:35 覆盖报告。

完成条件：覆盖预先声明的训练、验证、测试与预热区间；两条日线轨按 `code/date` 精确配对；
09:35 bar、前一日收盘、交易状态和 `as_of` 一致。任何不足都 fail closed。

### M8.4：真实 corporate action 映射

交付：回填 `query_dividend_data`，完成 corporate action normalizer、真实分红与送转 fixture，
以及 RQAlpha replay 对账。

完成条件：announcement、record、ex 和 pay date 不混用；至少一个真实现金分红和一个真实
送转场景与独立计算一致，且不会读取决策时点之后才公布的数据。

### M8.5：真实 replay 与 OOS artifact

交付：从本地冻结数据生成 `EvaluationPeriod` 的 runner，以及
`artifacts/backtests/<run-hash>/` 下的 immutable bundle：

```text
manifest.json
periods.parquet
report.json
```

完成条件：bundle 关联数据、策略、状态、分配器、成本和代码版本；默认 252/63 walk-forward
及预热期覆盖完整；报告包含全部 M4 统计证据。结果不要求盈利，但必须真实、扣费、可重放，
且不能因结果不理想而改写测试区间。

## 审阅时需要确认

1. Milestone 7 单项 schema pilot 和离线复验通过后，是否把 Milestone 8 调整为 active
   data-backfill milestone。
2. 在任何完整回填前冻结沪深 300+中证 500 的研究日期范围、训练/验证/测试边界和预热期。
3. 是否继续严格使用 provider `adjustflag=2`。本计划默认遵守 ADR-0004，不接受未经验证的
   本地派生价格；若未来改变，应单独提交 superseding ADR。
4. 是否接受 M7 CLI 的项目预算、最小间隔、session item 上限和异卷备份门禁。
5. `query_dividend_data` 的真实 corporate action 映射是否作为 M8 必交付物；本计划默认保留，
   因为没有真实分红/送转证据时无法完整验证回测账务。

## 完成与停止边界

- M8.0-M8.5 执行真实数据和评估；不能用 M7 CLI 的合成测试替代。
- 真实数据不足时继续显示 `not_configured`，不生成通过状态。
- M8.5 真实报告经人工审阅后，才可更新 `docs/progress.md` 判断 M4 是否关闭。
- M4 未关闭时不得安装 M6 timer、生成真实 forward observation 或启用 live AI proposal。
- 达到 M8.5 后应停止扩展 BaoStock API，先审阅数据质量和 OOS 证据；基本面、宏观和 ETF
  接口不应顺势混入当前实施范围。
