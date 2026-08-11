# 本地行情数据

行情 payload、规范化数据和质量报告都保留在本机，不提交到 Git。当前目录结构为：

```text
data/
  raw/           query-addressed、带 payload/envelope hash 的 provider 原始 envelope
  normalized/    固定 schema 的 Parquet 数据集
  features/      后续里程碑生成的版本化特征
```

每条规范化记录保留 `source`、`source_received_at`、payload SHA-256 和 raw
artifact 路径。决策查询必须显式传入带时区的 `as_of`；执行价格只使用不复权行情，
信号只使用前复权行情。决策快照按同一 `code/date` 严格配对 `adjustflag=2` 和
`adjustflag=3`；任一轨缺失、晚于 `as_of` 或状态不一致都会 fail closed。
市场状态另使用显式 `regime_codes`、不复权日线 `amount/turnover` 和决策日精确
09:35 的五分钟 bar；09:35 决策的日线查询截止前一完整交易日。状态 universe 与候选
shortlist 不得互相冒充，覆盖不足同样会 fail closed。BaoStock `turn` 按 provider 的
百分数单位原样保存，不应再乘以 100。
normalized 价格固定为 `decimal128(20,6)`；更高精度的 provider 价格使用固定
`ROUND_HALF_EVEN` 量化到六位，immutable raw 仍保留完整原值。

## API 接入状态与后续计划

现有离线数据层已接入 `query_stock_basic`、`query_trade_dates`、`query_all_stock`、
`query_history_k_data_plus` 和 `query_adjust_factor`。Milestone 7 下载器还实现了
`query_daily_history_k_AStock`、`query_daily_adjust_factor`、`query_hs300_stocks`、
`query_zz500_stocks` 和 `query_dividend_data` 的 endpoint contract、provider adapter
与 normalizer，并新增 point-in-time `index_memberships` 数据集。

`query_history_k_data_plus` 日线双价格和沪深300/中证500成分单项 pilot 已于 2026-08-10
通过。固定 2024-06-07 成分的 M8.0 基础集已于 2026-08-11 完成：800 个标的从 2011-01-01
至 2026-08-10 的前复权/不复权日线共 4,973,298 行。该固定成分数据有幸存者偏差，不能作为
正式 OOS 结论。API 优先级、请求计数风险、落盘 schema、程序消费路径和 M8.0-M8.4 计划见
[`docs/baostock-data-plan.md`](../docs/baostock-data-plan.md)。

批量回填的数据根目录为 `/mnt/exos_1t/quant/baostock`。JSONL 请求、严格串行下载、停止条件和
离线处理见 [`docs/baostock-download-operations.md`](../docs/baostock-download-operations.md)。
下载器不推断设备、mount 或空间，也不干预三块 EXOS 硬盘的 300 秒 `hd-idle` 策略。

用户提供的 BaoStock 官方 [blacklist 页面](https://www.baostock.com/blacklist) 给出单 IP 每日
`50,000` 次访问上限并禁止并发。M7 CLI 以跨 data root 的 global provider lock 强制串行，按
每个底层 socket send 计数，并默认在 40,000 次停止。该页面已于 2026-08-09 直接在线核实；
黑名单错误 `10001011` 写本地 marker 并禁止自动重试。官方没有声明自然日时区、具体计数口径
或 QPS。

## BaoStock 缓存和请求门禁

`trading-codex-data` 永久保持离线。兼容参数 `--fetch-missing` 只会在任何 provider import 或
login 前以退出码 `2` 拒绝。唯一联网入口是 `trading-codex-baostock`，它读取 JSONL exact
request、持有全局锁并把响应保存为 raw envelope。

先用离线命令确认缺口：

```bash
uv run trading-codex-data sync \
  --start-date 2024-06-03 \
  --end-date 2024-06-07 \
  --codes sh.600000 \
  --with-forward-adjusted-daily \
  --with-five-minute
```

`--with-forward-adjusted-daily` 是显式 opt-in；不传时仍只同步不复权日线。
`adjustflag=2` 和 `adjustflag=3` 使用不同的 exact-query cache key，不会互相冒充
cache hit。

已有 raw cache 仍可完全离线重放。改变 endpoint、日期范围、复权标志或 client version 会形成
新的 request ID。目标文件存在就跳过，内容好坏由独立 envelope 检查发现。M7 操作步骤见
[`docs/baostock-download-operations.md`](../docs/baostock-download-operations.md)。

## 本地质量检查

以下命令只读取本地 Parquet，不访问 BaoStock：

```bash
uv run trading-codex-data inspect-raw
uv run trading-codex-data ingest-raw

uv run trading-codex-data quality \
  --as-of 2026-08-08T12:00:00+08:00

uv run trading-codex-data assess-0935 \
  --start-date 2024-06-03 \
  --end-date 2024-06-07 \
  --codes sh.600000 \
  --as-of 2026-08-08T12:00:00+08:00
```

报告写入 `artifacts/data-quality/`。覆盖率只描述命令指定的标的和日期，不能从局部
样本外推到全市场或全历史。`assess-0935` 只有在 calendar、每日 historical
universe 和全部预期 09:35 bar 都完整时才返回 `status=passed`；否则列出
`missing_calendar_dates`、`missing_universe_dates` 或缺失标的日，并以退出码 `2`
fail closed。
