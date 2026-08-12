# BaoStock raw 下载器操作手册

`trading-codex-baostock` 只做一件事：读取 Trading Codex 生成的 JSONL 请求，阻塞、串行访问
BaoStock，把响应保存为 raw envelope。它不做计划冻结、空间预测、备份、Parquet 预处理、质量
验收或回测。

默认 data root 是 `/mnt/exos_1t/quant/baostock`。也可以用 `--data-root` 或
`BAOSTOCK_DATA_ROOT` 指向任意目录；给哪个目录就写到哪个目录。

## 最短用法

当前 data root 已有 2024-06-07 查询得到的沪深300和中证500共 800 个成分。生成与已完成
M8.0 完全相同的请求：800 个成分、2011-01-01 至 2026-08-10、前复权和不复权日线，以及
instruments、trade calendar：

```bash
cd /home/radxa/quant/trading-codex

UV_CACHE_DIR=/tmp/trading-codex-uv-cache \
uv run --frozen trading-codex-requirements base-daily \
  --data-root /mnt/exos_1t/quant/baostock \
  --snapshot-date 2024-06-07 \
  --start-date 2011-01-01 \
  --end-date 2026-08-10 \
  > /tmp/trading-codex-base-daily.jsonl
```

正常情况下得到 1,602 条 exact request：2 条基础数据请求，加 800 个代码的两条价格轨。查看前
几行和总数不访问网络：

```bash
sed -n '1,5p' /tmp/trading-codex-base-daily.jsonl
wc -l /tmp/trading-codex-base-daily.jsonl
```

开始下载：

```bash
UV_CACHE_DIR=/tmp/trading-codex-uv-cache \
uv run --frozen trading-codex-baostock \
  --data-root /mnt/exos_1t/quant/baostock \
  --requests /tmp/trading-codex-base-daily.jsonl
```

也可以直接使用 Unix 管道，不保存中间请求文件：

```bash
UV_CACHE_DIR=/tmp/trading-codex-uv-cache \
uv run --frozen trading-codex-requirements base-daily \
  --data-root /mnt/exos_1t/quant/baostock \
  --snapshot-date 2024-06-07 \
  --start-date 2011-01-01 \
  --end-date 2026-08-10 \
| UV_CACHE_DIR=/tmp/trading-codex-uv-cache \
  uv run --frozen trading-codex-baostock \
    --data-root /mnt/exos_1t/quant/baostock
```

中断后执行同一命令即可续传。请求对应的目标文件存在时直接跳过，不登录 BaoStock，也不读取或
校验该旧文件；目标文件不存在时才请求并落盘。

## 自定义请求

每行是一个独立 JSON object，只允许 `operation` 和 `query`：

```json
{"operation":"daily_bars","query":{"adjustflag":"2","code":"sh.600000","end_date":"2026-08-10","frequency":"d","start_date":"2011-01-01"}}
```

下载器按行顺序执行。支持的 operation 和字段由代码中的 BaoStock endpoint contract 决定；未知
operation、缺字段、多字段、非法日期、代码、frequency 或 adjustflag 会在联网前退出。

首次需要成分股时，可先生成两条指数成分请求：

```bash
uv run --frozen trading-codex-requirements index-memberships \
  --date 2024-06-07 \
  > /tmp/index-memberships.jsonl
```

下载后先执行下面的离线 ingest，`base-daily` 才能从 normalized index membership 生成成分列表。

## 文件与幂等规则

每个 exact request 的固定目标是：

```text
<data-root>/raw/baostock/<operation>/<request-id>.json
```

`request-id` 是 provider、BaoStock client version、operation 和 query 的 SHA-256。raw envelope
保存：

- `schema_version`、`source`、`operation` 和 exact `query`；
- provider 字段顺序与全部原始字符串行；
- 不含接收时间的 payload `content_sha256`；
- UTC `received_at`；
- 覆盖其余全部 envelope 元数据的 `envelope_sha256`。

新响应编码后先在内存中按 envelope 协议验证，再写同目录临时文件，完成 `fsync` 后 atomic
replace 到目标，最后由下载器从磁盘重新读取并检查 canonical JSON、两个 hash、固定文件地址、
endpoint 字段和行结构。任何写盘或自检错误立即退出且不重试。

新下载只写 schema v2。旧 pilot 的 schema v1 raw 仍可由离线工具读取，但 v1 只有 payload hash；
这项兼容不会放宽 v2 的完整地址、client version 或 envelope hash 检查。

已经存在的目标文件只代表“该请求不再由下载器发送”，不代表文件一定有效。坏文件不会在下载
阶段触发隐式重下；检查和处置属于离线数据工具。

## 离线检查与预处理

以下命令均不导入 BaoStock 网络 adapter：

```bash
uv run --frozen trading-codex-data \
  --data-root /mnt/exos_1t/quant/baostock \
  inspect-raw

uv run --frozen trading-codex-data \
  --data-root /mnt/exos_1t/quant/baostock \
  ingest-raw
```

`inspect-raw` 独立重新读取每个 envelope；坏 JSON、hash 或固定地址不匹配、query/字段/行结构
异常只进入 `warnings`，不会产生网络请求。

`ingest-raw` 再独立验证 envelope，运行 normalizer，并按 raw payload hash 写入固定 Parquet
segment。已存在的 payload 或 segment 会跳过；重复运行不会重复发布。之后再由 `quality`、覆盖
评估和回测工具执行各自的 fail-closed 检查。不同 payload 产生相同业务键时，ingest 拒绝后一个
segment 并报告 warning，不能先污染 normalized 再等待读取阶段发现重复。

下载和预处理互不传递完成状态：下载器自检不能替代 `inspect-raw`，`inspect-raw` 通过也不能
替代 normalized quality 或 M4 OOS 验收。

## M8.1 真实规模 EOD smoke

M8.1 不访问 BaoStock。它只读取已完成 M8.0 ingest 的 normalized Parquet，并使用隔离的
RQAlpha 6.3.0 环境。首次创建环境：

```bash
cd /home/radxa/quant/trading-codex

uv venv /tmp/trading-codex-rqalpha --python 3.12
uv pip install --python /tmp/trading-codex-rqalpha/bin/python -e .
uv pip install --python /tmp/trading-codex-rqalpha/bin/python \
  -r spikes/rqalpha/requirements.txt
```

复现 2026-08-11 验收范围：

```bash
/usr/bin/time -v /tmp/trading-codex-rqalpha/bin/python \
  -m trading_codex.backtest.m8_smoke \
  --data-root /mnt/exos_1t/quant/baostock \
  --universe-date 2024-06-07 \
  --start-date 2024-06-07 \
  --end-date 2026-08-10 \
  --material-as-of 2026-08-11T12:45:22.535136Z \
  --train-periods 252 \
  --test-periods 63 \
  --bootstrap-samples 1000
```

也可以使用同环境里的 `trading-codex-m8-smoke` entrypoint。运行会顺序执行
`turnover_10pct` 和 `turnover_20pct`，向 stderr 输出进度，并把内容寻址 JSON artifact 写入
`<data-root>/artifacts/m8.1/`。重复运行会产生新的时间和资源记录；研究 observations 与
walk-forward report 应保持一致。

该命令明确是固定成分 EOD 工程 smoke：有幸存者偏差，使用非官方等权 benchmark，不应用
corporate action，也不使用 09:35 opening 特征。输出中的 `formal_m4_oos=false` 不能被改写；
M4 正式报告仍需要 M8.2-M8.4 数据和冻结边界。

## 请求限制

BaoStock 官方规则是同一公网 IP 每日不得超过 50,000 次 API 请求，并禁止并发连接。程序执行：

- 全局非阻塞 `flock`；已有另一个下载进程时立即退出；
- 一个 login session 内严格顺序请求；
- 每次底层 socket send 发送前，追加到
  `~/.local/state/trading-codex/baostock/attempts/YYYY-MM-DD.jsonl`；
- 按 `Asia/Shanghai` 自然日计数，默认在 40,000 次停止，并为 logout 保留最后一次；
- 不提供并发、自动重试、rolling 24-hour、session/item 预算或人为最小间隔。

计数包含 login、query、pagination、logout 和发送失败。旧 M7 SQLite 账本中同一天的 attempt
也会加入总数，避免切换实现时把已发生请求归零。程序无法看到同一公网 IP 下其他机器或脚本的
请求，因此下载期间仍不得运行第二个 BaoStock 客户端。

达到 40,000 次时返回 `paused_daily_limit`，本地关闭 socket。次日重跑同一请求流，从缺失文件
继续。

收到 `10001011` 时立即停止并写：

```text
~/.local/state/trading-codex/baostock/provider-blacklisted.json
```

确认 BaoStock 管理员已解除黑名单后才能人工删除该 marker；跨日不会自动解除。

## 职责边界

- 下载器不检查剩余空间，也不知道响应最终有多大；真实目录创建、写入、`fsync` 或 replace
  失败时停止。
- backup target 不是下载前置条件。raw 下载完成后由独立备份流程复制并校验。
- 固定 2024-06-07 成分的 15 年数据可用于真实数据 smoke test、性能测量和首次展示，但有
  幸存者偏差，不能据此关闭 M4 正式 OOS 验收。
- 历史 5 分钟数据不属于默认基础集。精确 09:35 决策、历史 point-in-time universe、benchmark
  和 corporate action 仍按具体研究需求另外生成 request。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 全部完成、全部已存在，或达到日上限后正常暂停 |
| `2` | 请求流、计数文件或本地协议错误 |
| `3` | 实际目录或文件系统操作失败 |
| `4` | 另一个 BaoStock 下载进程持有全局锁 |
| `5` | provider 返回错误或响应 malformed |
| `6` | `10001011` 或本地 blacklist marker 硬停止 |
