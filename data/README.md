# 本地行情数据

行情 payload、规范化数据和质量报告都保留在本机，不提交到 Git。当前目录结构为：

```text
data/
  raw/           内容寻址且不可变的 provider 原始响应
  normalized/    固定 schema 的 Parquet 数据集
  features/      后续里程碑生成的版本化特征
```

每条规范化记录保留 `source`、`source_received_at`、payload SHA-256 和 raw
artifact 路径。决策查询必须显式传入带时区的 `as_of`；执行价格只使用不复权行情，
复权序列仅用于研究和信号，并保留到复权输入的关联。

## BaoStock 缓存和请求门禁

`sync` 默认完全离线。它先检查规范化数据覆盖和 exact-query raw cache；cache miss
会 fail closed，既不登录也不请求 BaoStock。只有显式添加 `--fetch-missing` 才允许
回源，而且客户端硬性限制每个进程最多尝试 1 次上游数据请求。失败的尝试同样消耗
预算，不能通过捕获异常在同一进程连续重试。

先用离线命令确认缺口：

```bash
uv run trading-codex-data sync \
  --start-date 2024-06-03 \
  --end-date 2024-06-07 \
  --codes sh.600000 \
  --with-five-minute
```

确实需要补缓存时，人工执行同一命令并追加一次 `--fetch-missing`。一次运行只会补
一个 exact query；遇到下一个 cache miss 后会停止。不要把该命令放入循环、并发任务
或面向大批标的的自动重试中。随后恢复离线命令，确认数据来自缓存且
`upstream_requests` 为 `0`。改变日期范围或 query 参数会形成新的 cache key。

## 本地质量检查

以下命令只读取本地 Parquet，不访问 BaoStock：

```bash
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
