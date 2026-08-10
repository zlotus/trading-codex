# BaoStock 安全下载器操作手册

`trading-codex-baostock` 是唯一允许访问 BaoStock 网络的项目入口。除 `fetch` 外，所有子命令
均为离线命令。Milestone 7 已完成；2026-08-10 的首个真实单项 pilot 已在
`/mnt/exos_1t/quant/baostock` 通过，完整回填仍属于 Milestone 8。

## 路径与门禁

- 默认 data root：`/mnt/exos_1t/quant/baostock`；可用 `--data-root` 或
  `BAOSTOCK_DATA_ROOT` 指向任意其他目录
- global provider state：`$XDG_STATE_HOME/trading-codex/baostock`，未设置 XDG 时使用
  `~/.local/state/trading-codex/baostock`
- data root 下的 `state/request-audit.sqlite` 只是 global state 的审计镜像，不能作为并发锁或
  预算权威源
- provider 硬上限：`50,000/IP/日`；项目硬上限：`45,000/日`
- 默认自然日和 rolling 24-hour 各 `2,000` attempts；默认 session 最多 `100` attempts
- socket 最小间隔为 3 秒；首个真实 pilot 已使用 30 秒，其余 endpoint 的 schema pilot 继续
  使用 30 秒，除非另行审阅并冻结

frozen manifest 可以在代码硬上限内显式调整默认预算，但该值参与语义 SHA-256，必须与完整
manifest 一起人工确认；`fetch` 没有临时预算覆盖参数。pilot 应使用示例中的 20 次小预算，不能
把 45,000 次项目硬上限当作日常目标。

同一公网 IP 下存在任何其他 BaoStock 客户端时，不得执行 `fetch`。命令不得放入 timer、循环、
自动重试、worker、thread 或并发 shell 中。

## 首次初始化

初始化会递归创建指定目录和固定布局，执行 write/fsync/atomic replace/flock 探针并初始化本机
global state，不访问 BaoStock：

```bash
uv run trading-codex-baostock \
  --data-root /mnt/exos_1t/quant/baostock \
  doctor --initialize --json
```

`--data-root` 是唯一的位置参数：程序不区分系统盘、数据盘或外置盘，也不检查 mount point、设备名
和 UUID。每次 fetch 前程序会重新执行 storage preflight；目标不可写、`fsync`/atomic replace/
`flock` 探针失败、可用空间低于 100 GiB、占用率达到 90%，或预计峰值加 20 GiB 余量后越界，
都会在 login 前停止。下载器不会调用 `hdparm`、`hd-idle -t` 或 remount，也不会用 keepalive I/O
阻止 300 秒自动休眠。

## 创建和冻结 pilot

pilot spec 示例：

```json
{
  "created_by": "operator-name",
  "boundaries": {
    "warmup": null,
    "train": null,
    "validation": null,
    "test": null
  },
  "limits": {
    "calendar_day_attempts": 20,
    "rolling_24h_attempts": 20,
    "session_attempts": 5,
    "minimum_interval_seconds": 30
  },
  "estimated_peak_bytes": 104857600,
  "items": [
    {
      "key": "daily-bars-pilot",
      "operation": "daily_bars",
      "query": {
        "code": "sh.600000",
        "start_date": "2011-01-01",
        "end_date": "2025-12-31",
        "frequency": "d",
        "adjustflag": "2"
      },
      "max_pages": 3,
      "max_attempts": 3,
      "dependencies": []
    }
  ]
}
```

```bash
uv run trading-codex-baostock \
  --data-root /mnt/exos_1t/quant/baostock \
  plan create --spec pilot-spec.json --json

uv run trading-codex-baostock \
  --data-root /mnt/exos_1t/quant/baostock \
  plan show --manifest /mnt/exos_1t/quant/baostock/manifests/draft/<manifest-id>.json \
  --json

uv run trading-codex-baostock \
  --data-root /mnt/exos_1t/quant/baostock \
  plan freeze \
  --manifest /mnt/exos_1t/quant/baostock/manifests/draft/<manifest-id>.json \
  --json
```

相同语义 spec 产生相同 manifest SHA-256。`created_at` 不参与语义 hash；endpoint、完整 query、
字段顺序、BaoStock client `00.9.30`、官方规则资源 hash、预算、分页和依赖均参与。draft 不能
fetch，frozen 文件被修改后 hash 校验会失败。manifest 还必须保持无重复键的 canonical JSON；
仅重新排版或加入歧义键也会被拒绝。

日线 item 可以覆盖完整显式日期区间，不需要按年份拆成多个高层 API 调用。BaoStock 每页最多
2,000 行，`ResultData.next()` 跨页时仍会产生新的底层 socket attempt；因此 15 年 pilot 明确
冻结 `max_pages=3`，程序会逐次计数并在第 4 次发送前停止。分钟线数据量更大，仍限制为每个
item 最多 31 个自然日。

## 人工执行单项 fetch

先用 `status` 确认 cache miss、attempt 余额和依赖，再由操作者逐字核对 frozen hash：

```bash
uv run trading-codex-baostock \
  --data-root /mnt/exos_1t/quant/baostock \
  status \
  --manifest /mnt/exos_1t/quant/baostock/manifests/frozen/<manifest-id>.json \
  --json

uv run trading-codex-baostock \
  --data-root /mnt/exos_1t/quant/baostock \
  fetch \
  --manifest /mnt/exos_1t/quant/baostock/manifests/frozen/<manifest-id>.json \
  --confirm-manifest-sha256 <sha256> \
  --max-items 1 \
  --json
```

fetch 在 login 前获取全局非阻塞锁。每次 `send_msg` 在发送前写入 attempt；login、query、每个
page、logout 和发送异常都消耗预算。cache hit 不导入 BaoStock、不 login、消耗 0 attempts。
任一失败会停止队列且不会自动重试；需要人工分析后冻结新的 retry manifest。

同一 data root 还使用独立非阻塞锁；`fetch` 不能与 `status`、`sync`、`verify`、`import-raw`
或 `recover` 交叠。该锁不替代跨 data root 的 global provider lock。

## 离线同步与校验

```bash
uv run trading-codex-baostock \
  --data-root /mnt/exos_1t/quant/baostock \
  sync \
  --manifest /mnt/exos_1t/quant/baostock/manifests/frozen/<manifest-id>.json \
  --json

uv run trading-codex-baostock \
  --data-root /mnt/exos_1t/quant/baostock \
  verify \
  --manifest /mnt/exos_1t/quant/baostock/manifests/frozen/<manifest-id>.json \
  --as-of 2026-08-09T15:00:00+08:00 \
  --json
```

sync 只读 immutable raw，并以 data-root 排他锁写 staging。每个 frozen manifest、每个 dataset
最多原子发布一个 100 万行且不超过 2 GiB 的 segment；超过时必须拆 manifest。已发布 segment
只新增，不覆盖；既有单文件 Parquet 保持为只读基线。业务主键相同但值不同会写入 quarantine，
不会静默覆盖。verify 检查 raw hash、query index、字段、normalizer、segment schema、主键、
provenance、quarantine 和 item 状态，通过后写 completion receipt。

normalized `PRICE` 固定为 `decimal128(20,6)`。BaoStock 返回更多小数位时，normalizer 使用
`ROUND_HALF_EVEN` 显式量化到六位；immutable raw 保留 provider 原值，verify 使用同一规则重放。

`verify` 会读取每个 Parquet 文件的物理 schema，拒绝跨 legacy/segment 的重复业务键，并逐行
确认 raw 重新运行 normalizer 后的结果完整存在；仅有一个相同 payload hash 的行不足以通过。
verify report 使用报告内容 SHA-256 命名并只新增；同一 manifest 的首次通过会冻结唯一
completion receipt。重复验证必须使用相同 `--as-of` 和相同数据，任何试图用另一结果覆盖
receipt 的操作都会 fail closed。
BaoStock dividend 只提供公告日期，当前 `available_at` 保守使用公告日后一个自然日 00:00
（`Asia/Shanghai`），避免把公告日内未知的发布时间当作盘前可见。

旧仓库 raw cache 只能在操作者确认其由 `00.9.30` 生成后离线导入：

```bash
uv run trading-codex-baostock \
  --data-root /mnt/exos_1t/quant/baostock \
  import-raw \
  --source-root /home/radxa/quant/trading-codex/data/raw \
  --source-provider-client-version 00.9.30 \
  --json
```

## 故障与恢复

进程崩溃留下未关闭 session 时，后续 fetch 会 fail closed。人工核对 raw、attempt 和进程状态后，
只能追加 abandoned 事件。恢复命令也必须取得 data-root lock 和 global provider lock；若仍有
fetch 或离线发布进程存活，它会拒绝修改状态：

```bash
uv run trading-codex-baostock recover session \
  --session-id <session-id> \
  --operator <operator> \
  --reason <review-result> \
  --json
```

收到 `10001011` 时，程序先持久化 `provider_blacklisted` incident，然后本地关闭 socket，不发送
logout。跨日和 cooldown 到期均不会解除。操作者必须先按官方说明联系管理员并确认解除，再追加：

```bash
uv run trading-codex-baostock recover blacklist \
  --incident-id <incident-id> \
  --operator <operator> \
  --administrator-confirmation <confirmation-record> \
  --reason <resolution> \
  --json
```

恢复事件不会删除 attempt、incident 或重置预算。

首个 `query_history_k_data_plus` 日线 pilot 已通过：manifest `bs-af5dfdaa19fc5c6ae075` 使用
`adjustflag=2`、2011-2025 日期范围、30 秒间隔和 `--max-items 1`，4 次 socket attempt 得到
3,644 行 raw；sync/verify 为 0 duplicate、0 missing、0 mismatch、0 quarantine。尚未确定异卷
backup target；任何 bulk wave 前必须另行冻结备份目标、复制清单和 hash 复验流程。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 命令完成 |
| `2` | manifest、预算、state 或离线质量门禁阻断 |
| `3` | 目录、空间、fsync 或文件系统操作失败 |
| `4` | global provider lock 已被占用 |
| `5` | provider 返回错误或响应 malformed |
| `6` | `10001011` 黑名单硬停止 |
