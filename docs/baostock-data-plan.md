# BaoStock 数据接入与 Milestone 8 计划

- 状态：Active
- 计划版本：`baostock-data-plan-v3`
- BaoStock client：`00.9.30`
- data root：`/mnt/exos_1t/quant/baostock`

M8 的当前目标是先补齐可运行的真实基础数据，让 M4 决策、回测和 M5/M6 的数据相关分析在真实
规模上运行；随后再补正式 OOS 所需的 point-in-time 数据。网络下载边界遵循 ADR-0010，研究
需求、下载、预处理、质量和回测是相互独立的步骤。

## 数据流

```text
Trading Codex requirement
  -> JSONL exact request
  -> serial raw-only downloader
  -> versioned raw envelope
  -> independent inspect / ingest
  -> normalized Parquet
  -> quality / coverage / explicit as_of
  -> DecisionSnapshot / RQAlpha / HistoricalReplay
  -> immutable analysis artifact
```

下载器只根据 query-addressed 目标文件是否存在决定跳过。每个下游阶段重新验证自己的输入，不能
把上游命令的 `passed` 当作数据可用证明。回测、主应用和 scheduler 都不能隐式访问 BaoStock。

## 官方限制与项目执行

BaoStock 官方 blacklist 页面明确：每日 API 请求不得超过 50,000 次，禁止并发连接，命中
`10001011` 后联系管理员。页面没有定义 QPS、自然日时区、分页和 login/logout 是否计数。

项目执行规则：

- 同一用户的全局非阻塞锁保证只有一个下载进程；
- 每次底层 socket send 在发送前计数，按 `Asia/Shanghai` 自然日记录；
- 默认 40,000 次停止，为官方上限和同 IP 的不可见流量留余量；
- 严格顺序，无自动重试；普通错误在第一处停止；
- 下载期间不得运行同一公网 IP 下的其他 BaoStock 客户端。

## P0：默认基础数据集

默认用于首次展示、真实数据 smoke 和性能测量，不用于正式 OOS 结论：

- universe：2024-06-07 查询的沪深300成分和中证500成分并集，共 800 个唯一股票；
- 日期：2011-01-01 至 2026-08-10；
- 日线双轨：`adjustflag=2` 前复权信号价、`adjustflag=3` 不复权执行价；
- 基础维表：全部 instruments、完整 trade calendar；
- 默认不下载分钟线、财务报表或宏观数据。

对应 1,602 条高层 request：2 条基础数据，加 800 个代码的两条日线。15 年日线通常会分页，
实际 socket attempt 高于 1,602，但仍显著低于 40,000 日停止线。程序以真实 socket send 计数，
不依赖预估。

| API | normalized 数据 | 用途 |
| --- | --- | --- |
| `query_hs300_stocks(date)` | `index_memberships` | 生成固定沪深300成分快照 |
| `query_zz500_stocks(date)` | `index_memberships` | 生成固定中证500成分快照 |
| `query_stock_basic` | `instruments` | 代码、名称、上市/退市和证券类型 |
| `query_trade_dates` | `trade_calendar` | 交易日网格和 walk-forward 日期 |
| `query_history_k_data_plus` 日线 | `daily_bars` | 双价格、成交量额、换手、停牌和 ST |

固定当前成分向前回看 15 年有幸存者偏差。P0 只回答“代码能否在真实规模运行、数据接口和成本模型
是否接通”，不能回答策略是否有可推广收益。

## P1：关闭 M4 所需数据

正式 M4 OOS 需要在测试结果不可见前声明日期与参数，并补齐：

| API/数据 | normalized 数据 | 程序用途 |
| --- | --- | --- |
| 历史 `query_hs300_stocks(date)` / `query_zz500_stocks(date)` | `index_memberships` | 决策时点可见的候选和状态 universe |
| `query_all_stock(day)` | `historical_universe` | 当日上市、停牌和交易状态 |
| `query_history_k_data_plus` 双日线 | `daily_bars` | 信号价与执行价严格配对 |
| 指数日线 | benchmark dataset 或明确 index schema | benchmark return、alpha 和 beta |
| `query_adjust_factor` / `query_daily_adjust_factor` | `adjustment_factors` | corporate action 与双价格一致性检查 |
| `query_dividend_data` | `corporate_actions` | 分红、送转和 RQAlpha 账务验证 |
| 必要范围的 5 分钟线 | `five_minute_bars` | 只支持精确 09:35 决策和当日执行点 |

日频 EOD 研究不因“可能以后用到”而默认下载分钟线。只有使用 09:35 决策 contract 的分析才生成
相应请求，并按证券和短日期范围切片。

BaoStock dividend 只有公告日期，没有公告时刻。当前 normalizer 保守使用公告日后一个自然日
00:00 作为 `available_at`；引入更精确的公告时间需要独立可审计来源和新 contract。

## P2：交易与暴露研究

在 M4 真实 OOS 完成后再考虑：

| API | 可能的数据集 | 限定用途 |
| --- | --- | --- |
| `query_stock_industry` | `industry_memberships` | 行业暴露和报告切片，首版不进入硬风险 |
| `query_daily_history_k_ETF` | ETF 日线 | 经批准的现金替代或 ETF benchmark |
| 复权因子批量接口 | `adjustment_factors` | 大规模校验和缺口修复 |

## P3：暂缓接口

- `query_profit_data`、`query_operation_data`、`query_growth_data`、`query_dupont_data`；
- `query_balance_data`、`query_cash_flow_data`；
- `query_performance_express_report`、`query_forecast_report`；
- 利率、准备金率、货币供应量等宏观接口。

这些数据需要公告时间、修订历史、缺失策略和 point-in-time contract。它们不能解除当前 M4
阻塞，因此不进入默认请求集。

## Raw envelope 与 normalized

raw envelope 保存 provider 返回的原始字符串，不在下载阶段量化、补值或解释。payload hash 不含
接收时间，同一响应可以稳定识别；envelope hash 覆盖 payload hash 和接收时间等全部元数据；
文件名由 exact request 决定，同一 query 重跑不会新增文件。

normalized 每行继续保存：

- `available_at`；
- `source_received_at`；
- `source_payload_sha256`；
- 相对 `raw_artifact` 路径。

`ingest-raw` 按 payload hash 生成 immutable segment。坏 envelope、normalizer 错误或现有
normalized schema 异常只报告 warning/阻断对应数据集，不访问 provider。跨 payload 业务键冲突
也会在发布前拒绝。策略读取仍拒绝缺失、future、重复、双价格不一致或覆盖不足的数据。

## Milestone 8 切片

### M8.0：默认基础集回填

交付：生成 1,602 条请求，顺序下载全部缺失 raw，执行 envelope inspect 和 idempotent ingest。

完成条件：800 个成分的双价格日线、instruments 和 trade calendar 均可本地读取；记录实际行数、
磁盘占用、segment 数量、读取峰值内存和耗时；坏文件或缺口有明确列表。

状态：2026-08-11 已完成。日线 1,600 个 segment、双轨各 2,486,649 行；instruments 8,887 行、
trade calendar 5,701 行、index memberships 800 行。完整读取耗时 16:43.49、峰值 RSS 约
12.3 GiB；两份旧 pilot overlap 和 2 个 `tradestatus=0` 但有正成交量的标的日已明确记录。

### M8.1：真实规模 smoke runner

交付：显式 `fixed_snapshot_universe` 的 EOD runner，使用真实双价格、交易成本、M4 共享决策管线
和 RQAlpha adapter；输出带 `survivorship_bias=true` 的可重放 artifact。

完成条件：默认 252/63 walk-forward 可以完成，结果包含扣费后表现、回撤、regime slice、参数
敏感性和 benchmark 对比。结果不要求盈利，也不能作为 M4 正式完成证据。

### M8.2：point-in-time universe 与 benchmark

交付：预先声明的历史成分、historical universe、benchmark 日期网格和覆盖报告。

完成条件：每个决策只读取当时可见成分；缺一日 universe 或 benchmark 都阻止 OOS 报告。

### M8.3：corporate action 与必要的 09:35

交付：真实分红/送转映射，以及仅针对已批准 09:35 研究范围的分钟线。

完成条件：至少一个现金分红和一个送转样本独立对账；双价格、前一收盘、09:35 bar 和 `as_of`
一致。

### M8.4：正式 OOS artifact

交付：冻结数据描述、策略配置、代码版本、成本和参数网格，生成 untouched OOS 报告。

完成条件：默认 252/63 walk-forward 及预热覆盖完整，报告可重放且不能因结果不理想改写测试区间。
经人工审阅后才能判断 M4 是否关闭。

## M5/M6 边界

- M5 的 LLM key/provider 继续暂缓；历史数据回填不需要模型。
- M6 代码 contract 可继续用合成测试验证，但真实启用仍要求 M4 正式 OOS、live provider/task 和
  连续 60 个交易日 observation。
- 历史下载不能伪造 forward observation，也不能替代 09:35 live provider。
