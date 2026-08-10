# Milestone 7：BaoStock 外挂下载同步 CLI

- 状态：`completed`
- 目标数据根目录：`/mnt/exos_1t/quant/baostock`
- 命令：`trading-codex-baostock`
- 实施切片：M7.0-M7.5
- 计划版本：`baostock-download-plan-v3`

本里程碑只交付一个独立于 API/Web/策略/回测进程的 BaoStock 下载同步命令行程序。它负责
生成冻结清单、严格串行下载 immutable raw、从本地 raw 同步 normalized Parquet、校验和
断点恢复。真实全量回填、数据消费和 OOS 验收属于后续 Milestone 8，不能混入 M7 完成声明。

API 优先级与程序消费路径见 [`baostock-data-plan.md`](baostock-data-plan.md)。CLI 的代码、
离线验收和首个 30 秒间隔、`--max-items 1` 的真实 pilot 均已完成。

## 当前结论

- 2026-08-10 的真实 pilot 发起 4 次串行 socket attempt，全部返回 provider code `0`；
  `/mnt/exos_1t/quant/baostock` 已保存 raw、normalized segment、verify report 和 receipt。
- pilot preflight 显示挂载点总容量约 `1 TB`、可用约 `183 GB`、占用率约 `81.7%`。容量是
  易变状态，每个 manifest item 执行前仍必须重查。
- 用户已在宿主机实际执行 `mkdir`，确认 `/mnt/exos_1t` 可写；系统启动日志也记录
  `ntfs-3g` 以 read-write 挂载。当前 agent 看到的 `ro` 是工作区外路径的沙箱投影，不能
  用来判断宿主机目录状态。M7 只接受 `--data-root`，并在每次会话前验证该目录的实际写入、
  `fsync`、atomic replace、`flock` 和空间边界，不识别设备类别或 UUID。
- `/etc/default/hd-idle` 已配置 `sda`、`sdb`、`sdc` 空闲 `300` 秒后休眠，系统日志确认
  `sdc` 会正常 spin up/down。这是预期电源策略，不是数据盘故障。
- 旧 `CachedBaoStockClient` 不再隐式加载真实 provider，`trading-codex-data --fetch-missing`
  已永久阻断。新下载器在底层 `send_msg` 前记录 login、query、pagination、logout 和失败
  attempt，并使用跨 data root 的全局锁、持久 cooldown、双时间预算与 frozen manifest。
- normalized 回填使用每个 manifest、每个 dataset 一个 immutable Parquet segment，单 segment
  最多 100 万行且不超过 2 GiB；既有单文件 Parquet 保持为只读基线，不再由 M7 bulk sync
  反复重写。物理 schema、跨 segment 重复键和 raw 重放逐行一致性会 fail closed。
- 同一 data root 上的 `fetch`、`status`、`sync`、`verify`、`import-raw` 和 `recover` 共用排他锁，
  raw 提交不能与离线发布或恢复动作交叠。verify 报告按内容 SHA-256 只新增；首次通过产生的
  completion receipt 不可覆盖。
- 2026-08-09 已直接在线核实 BaoStock 官方 blacklist 页面。原文要求“每日API请求不能超过
  5万次，并且不能并发连接访问，超过后进入黑名单控制”；页面同时给出黑名单错误码和人工
  解除方式。页面没有说明自然日时区、高层 API 与底层 socket 的计数关系或具体 QPS。
- 完整回填的日期范围、训练/验证/测试边界和预热区间尚未冻结。除已定义的单证券 15 年日线
  schema pilot 外，不能先下载再根据结果选择测试区间。

因此，M7 单项 pilot 已闭环，但不能据此开始无人值守或系统性下载。下一步是冻结异卷 backup
target 和 M8.0 的数据边界，再对其余 endpoint 逐个执行独立 schema pilot；旧
`sync --fetch-missing` 仍不可使用。

## Provider 强制限制

规范来源：BaoStock 官方 [blacklist 页面](https://www.baostock.com/blacklist)，由用户于
2026-08-09 提供并批准联网访问，同日已直接在线核实。页面原文与程序示例确认：

1. “每日API请求不能超过5万次，并且不能并发连接访问，超过后进入黑名单控制”。
2. 黑名单响应为 `error_code="10001011"`、`error_msg="黑名单用户，请与管理员联系"`。
3. “黑名单控制后，请在QQ群联系管理员寻求解决，并告知互联网IP地址”。

官方页面没有说明自然日使用哪个时区，也没有定义 login、logout、分页或失败发送是否分别计入
访问次数，没有给出 QPS 或最小请求间隔。M7 因此采用以下**项目保守策略**，不得写成官方口径：

1. provider 上限按同一公网 IP 的自然日 `50,000` 次执行，不允许任何并发连接。
2. CLI 把每次底层 `send_msg` attempt 都计数，包括 login、首个 query、每个 pagination、
   logout 和失败发送；失败和进程崩溃不返还预算。
3. 自然日按 `Asia/Shanghai` 计算，同时维护 rolling 24-hour 预算，防止午夜前后突发翻倍。
4. provider 常量 `50_000` 不可由命令行或 manifest 提高。项目默认预算为每天及 rolling
   24-hour 各 `2,000` 次，项目硬上限为 `45,000` 次，为同 IP 的不可见流量保留至少 `5,000`
   次余量。frozen manifest 可在硬上限内显式选择更低或更高的项目预算，语义 hash 和人工确认
   使该调整可审计；临时 CLI 参数不能覆盖预算。
5. 默认 socket 最小间隔为 `3` 秒且不可通过 CLI 调低；首轮 pilot 使用 `30` 秒。
6. 任一响应出现 `10001011` 时，立即终止当前 session 和当日全部 live fetch，本地关闭 socket
   而不再发送 logout，落盘硬停止状态并禁止自动重试。只有管理员解除控制、操作者记录处置
   结果并执行受审计的人工恢复动作后，才能显式批准后续新会话；恢复动作不能重置任何预算。

CLI 只能统计自己的访问，不能发现同一 NAT/公网 IP 下其他主机或独立脚本。因此运行
`trading-codex-baostock fetch` 时，操作规程必须禁止任何其他 BaoStock 客户端；若出口 IP
共享且无法协调，必须进一步下调项目预算或停止运行。

## 目录职责

下载器已在目标 data root 初始化以下目录：

```text
/mnt/exos_1t/quant/baostock/
  raw/
    baostock/<operation>/<content-sha256>.json
    .query-cache/baostock/<operation>/<query-sha256>.json
  normalized/
  state/                         # 保存 data-root 镜像审计
    request-audit.sqlite
  manifests/
    draft/
    frozen/
    completed/
  reports/                       # M7 质量、覆盖与 backfill 报告
    data-quality/
    coverage/
    backfill/
  quarantine/
  tmp/                           # 同一文件系统上的原子写 staging
  backup-manifests/              # 仅保存异卷备份证据
```

| 目录 | 权威性与写入规则 | 恢复责任 |
| --- | --- | --- |
| `raw/baostock/` | 内容寻址、只新增、永不原地改写 | 最高优先级备份；所有 normalized 数据可由它重建 |
| `raw/.query-cache/` | exact query 到 raw hash 的可重建索引 | 校验失败时由 raw 扫描重建，不能冒充 raw 本体 |
| `normalized/` | 固定 schema、显式 provenance 和 `available_at` | 可重建，但每个完成波次仍应做快照以缩短恢复时间 |
| `state/` | 全局 provider state 的 data-root 镜像和 manifest attempt 审计 | 可由 global state 重新生成；global state 缺失或损坏时禁止联网 |
| `manifests/` | 下载范围、exact 参数、预算和状态机 | `frozen/` 只新增；修改范围必须生成新 manifest |
| `reports/` | 质量、覆盖和 backfill 校验输出 | 与对应 manifest/hash 一起保留 |
| `quarantine/` | schema 漂移、冲突或不完整响应及失败原因 | 永不进入 normalized；人工审阅后才能重试 |
| `tmp/` | 当前 sync 的临时文件 | 可重建；sync 结束时清理本次 manifest 的 staging |
| `backup-manifests/` | 记录异卷备份目标、文件 hash 和验证结果 | 与 data root 同一文件系统的副本不算备份 |

`raw_artifact` 继续保存相对 `raw/` 的路径，避免从仓库内 `data/` 迁移到目标 data root 后 provenance
失效。下载前应先离线导入并逐个校验当前仓库已有的 raw cache，不重复请求 BaoStock；
normalized 数据应从导入后的 raw 重放，或在行数、schema、hash 一致时做一次受控迁移。

并发锁、持久 cooldown、自然日/rolling 24-hour 计数器必须放在本机唯一的 global provider
state root，而不是由每个 data root 自己选择，否则两个不同数据目录可以同时联网。M7 已将
该路径冻结为 XDG state 下的 provider 唯一路径；data root 的 `state/` 只保存镜像审计。global state 不可用、损坏或
指向多个 provider identity 时，`fetch` 必须在 login 前退出。

## 存储前置检查

每次计划或 fetch 前必须检查：

1. executor 对 `--data-root` 指定的目录可写，并已通过 create、fsync、atomic replace 和跨进程
   排他锁探针。下载器不区分系统盘、数据盘或外置盘，也不判断 mount point、设备名和 UUID。
   该探针必须在 BaoStock login 前完成；磁盘唤醒发生在 provider session 和请求计数开始前。
2. 因默认目标当前是 `ntfs-3g`/FUSE 文件系统，首次真实 `doctor --initialize` 必须实际验证 fsync、
   atomic replace 和 `flock`。global provider lock/counter 使用本机文件系统中的 SQLite WAL；
   data root 通过 SQLite online backup 保存审计镜像，不在 FUSE 上承担全局事务状态。
3. 可用空间低于 `150 GiB` 时告警；低于 `100 GiB`、占用率达到 `90%`，或本波次预计峰值
   加 `20 GiB` 安全余量后会越过 `100 GiB` 时，必须在联网前停止。
4. 不自动删除 raw、quarantine 或历史 manifest 腾空间；空间不足需要人工决定扩容、异卷
   备份或缩小 manifest。

当前容量只允许进入估算和 pilot，不足以证明完整历史回填一定装得下。每个 API 的单日/单证券
pilot 必须记录 raw JSON、Parquet 和 staging 的实际字节数，再计算整波次上界。
M7 v1 为严格检查跨 segment 重复键会扫描当前 dataset；完整历史达到大规模前，M8 必须同时记录
峰值内存和校验耗时。若 ARM64 主机不能在边界内完成，应先加入 DuckDB/磁盘索引，不得靠跳过
全局查重继续回填。

## 与 hd-idle 协同

下载器不修改或绕过现有 `hd-idle` 服务，也不使用无意义的 keepalive I/O 阻止休眠：

1. 人工开始一个下载会话时，先用本地 preflight 唤醒 `/mnt/exos_1t`，等待目标目录读写、
   fsync 和锁探针完成，再建立 BaoStock 连接。
2. 首个 pilot 会话只允许 `1` 个 item。后续经批准的 `fetch --max-items N` 也只在一个进程、
   一个 login session 内顺序处理冻结 item，代码上限为 100，不建立 worker、thread 或并行 socket。
3. 若 item 之间停顿超过 300 秒，下一次 `fetch` 重新执行完整 storage preflight；程序不靠
   周期性触盘维持旋转。连续 item 的正常 raw/state 写入会自然保持当前数据盘活跃。
4. 异卷备份集中在同一人工会话末尾执行：只唤醒已选择的 backup target，不同时探测三块盘。
   备份和 hash 复验完成后停止 I/O，让 `hd-idle` 自行休眠。
5. storage wake/read/write 失败时在 login 前 fail closed。executor 不调用 `hd-idle -t`、
   `hdparm` 或 remount 命令干预设备状态，也不推断设备 identity。

这样既保持 BaoStock 请求严格串行，也避免把少量 item 分散到全天、反复触发硬盘启停。

## Manifest 与请求状态机

当前 schema v1 的每个 frozen manifest 保存：

- `manifest_id`、semantic SHA-256、schema version、创建时间和创建者；
- BaoStock client version、官方规则资源 SHA-256、endpoint、完整参数和固定字段顺序；
- 由操作者预先声明的预热、训练、验证和测试边界文本；
- 每个 item 的最大页数、最大 attempts、前驱依赖，以及全局自然日、rolling 24-hour、session
  和 cooldown 限制；
- 本波次预计峰值空间。业务日期、universe、benchmark 和代码生成输入必须在 M8 spec 中展开为
  exact item，不能由 fetch 运行时动态生成。

manifest 文件必须是无重复键的 canonical JSON；语义 hash 正确但文件重新排版、包含重复键或
非有限 JSON 数值时仍会 fail closed，保证人工审阅的字节内容与程序解析结果一致。

item 的 `fetching -> raw_committed -> normalized -> completed` 等状态保存在 global append-only
SQLite 账本中，不写回 frozen manifest。schema v1 不包含自动执行窗口、storage wake timeout 或
backup target；这些属于操作规程与 M8 bulk wave 的外部批准条件，不能从字段缺失推断为已批准。

任何失败都把 item 置为 `blocked` 或 `quarantined`，不能自动改成新参数重试。进程崩溃后只允许
根据 raw hash 和 append-only attempt 记录恢复；不能仅凭 normalized 文件存在就标记完成。

## 请求安全策略

M7 executor 必须满足：

1. 同一 data root 先获取非阻塞 data-root lock，再在 login 前获取 global provider lock；前者
   排除 raw/normalized/recovery 交叠，后者跨 manifest 和 data root 排除并发 socket。global lock
   持续持有到正常 logout 完成，或黑名单路径已本地
   关闭 socket 且 session 终态落盘；第二个进程即使使用不同 manifest 或 data root，也必须在
   任何 socket 发送前退出。程序不提供 `--workers`、并发 API、background daemon 或 timer。
2. `fetch` 只接受 frozen manifest。默认 `--max-items 1`，单命令 item 硬上限为 `100`；
   session 内允许一个有界顺序循环，但不能自动重试失败 item，也不能动态追加 query。
3. cache hit 不登录、不消耗网络预算。需要网络时，在最底层 `send_msg` 前原子预占预算并执行
   持久 cooldown，再分别记录 login、query、每次分页和 logout。失败或进程崩溃不返还预算。
4. 同时执行 `Asia/Shanghai` 自然日、rolling 24-hour、manifest、session 和单 item 五层预算；
   任何一层不足以覆盖预计 query/page/logout 时，都在 login 或下一次发送前 fail closed。
5. provider `50,000`、项目 `45,000` 和最小 `3 秒` 是代码硬边界；默认 `2,000` 预算可由
   frozen manifest 在不超过硬边界时显式调整，临时 CLI 参数不能提权。真实 pilot 必须继续
   使用远低于默认值的预算和 30 秒间隔。
6. 可能分页的 endpoint 必须声明页数上界。日频单证券 chunk 默认不超过 `1,500` 个预期交易日；
   五分钟 chunk 默认不超过 `20` 个交易日，给 2,000 行页面上限留出余量。
7. 不同 endpoint、日期范围、`adjustflag`、字段集合或 provider/client version 使用不同 cache key。
8. global state 缺失、损坏、时钟回退、锁语义异常或发现未关闭 session 时，等待恢复冷却并
   fail closed；不得提供清零当日计数的日常命令。
9. 任一 socket 响应出现 `10001011` 时立即写入 `provider_blacklisted` 硬停止状态，本地关闭
   socket 且不再调用 logout；后续 `fetch` 必须在 login 前拒绝，且不能由 cooldown 到期、跨日
   或更换 manifest 自动解除。M7.0 已定义追加式人工恢复事件，禁止删除 incident 或清零预算。

## Milestone 7 实施切片

### M7.0：外部规则快照与 CLI contract（已完成）

交付：保存 2026-08-09 已在线核实的 blacklist 页面原文、抓取元数据和 SHA-256；确定访问计数
口径、`Asia/Shanghai` 与 rolling 24-hour 预算、`10001011` 硬停止状态、global state identity、
稳定退出码和 JSON 输出 schema；在 `pyproject.toml` 注册 `trading-codex-baostock`，但默认所有
命令都不联网。

完成条件：官方 `50,000/IP/日` 与禁止并发成为不可上调的代码常量；`10001011` 只能进入人工
解除的硬停止状态；文档、CLI help、页面快照和测试口径一致。未来页面内容变化时先停下修订
contract，不能自动选择更宽松解释。

### M7.1：存储、global state 与请求门禁（代码与离线验收完成）

交付：`doctor`、任意 data root 目录初始化、space/fsync/atomic replace/flock 探针、global provider
lock、append-before-send attempt ledger、自然日及 rolling 24-hour 计数、持久 cooldown、
`hd-idle` pre-wake 和 crash recovery。

完成条件：两个进程、两个 data root 和进程重启均不能并发或绕过预算；损坏 state、磁盘离线、
低空间和时钟回退在 login 前 fail closed。测试只使用 fake clock/socket。

真实 `/mnt/exos_1t/quant/baostock` 的 NTFS/FUSE `fsync`/atomic replace/`flock` 探针尚未执行，
属于 M7.5 live pilot 前置验收，不由临时本地文件系统测试替代；无需 mount identity 证据。

### M7.2：离线 planner 与 endpoint manifest（已完成）

交付：`plan create/show/freeze` 从操作者审阅的 spec 生成 exact-query item、语义 hash、依赖、
页数和空间上界；`status` 离线报告 cache hit/miss 与 attempt。首版 adapter 覆盖现有 API 及
P1/P2 必需的批量日线、批量因子、沪深 300、中证 500 和 dividend endpoint；每个 endpoint
都有固定字段 contract 和 fixture。根据研究 coverage 自动生成完整 M8 spec 不属于 M7 v1。

完成条件：所有 plan 命令保证零网络；相同输入生成相同 manifest hash；draft 不能 fetch；超页
item 必须在 freeze 前切片，不能依赖运行时无限 pagination。

### M7.3：严格串行 raw fetch（代码与 fake-socket 验收完成）

交付：唯一联网命令 `fetch`。它获取 data-root lock 和 global provider lock，预占
login/query/page/logout 预算，在一个 login session 内按 manifest 顺序处理不超过
`--max-items` 的 item，并把响应内容寻址写入 raw、追加 attempt/checkpoint 后退出。它不执行
策略、回测或 normalized merge。

完成条件：第二个 fetch 在 socket 前失败；cache hit 为零访问；所有失败 attempt 计数；达到
任一预算、provider error、schema drift 或 raw fsync 失败立即停止，已落盘 item 可重放且不会
重复下载；`10001011` 会锁住后续 fetch，跨日和自动重试均不能绕过。

### M7.4：完全离线 sync、verify 与 immutable segments（已完成）

交付：`sync` 从 frozen manifest 和本地 raw 构建 staging Parquet，再原子发布有界 immutable
segment；`verify` 检查 raw hash、query index、物理 schema、主键冲突、provenance、raw 重放
逐行一致性和 manifest 完成度；verify 报告使用内容 hash 命名，首次通过的 completion receipt
只新增、不覆盖；提供现有仓库 raw cache 的逐 artifact 离线校验与 import。冲突进入 quarantine，
不静默覆盖。

完成条件：`sync`、`verify`、`status` 和 `plan` 即使 cache miss 也不能 import/login BaoStock；
重复运行产生相同 normalized 内容；中断 segment 发布不损坏已发布 normalized 数据。全历史
日期和 OOS coverage 报告属于 M8，不由 M7 manifest 完成度冒充。

### M7.5：运维验收与单项 pilot（已完成）

交付：稳定 `--json` 状态、预算/锁/manifest 可观测性、操作文档、ARM64 安装验证、并发和
44,999/45,000 项目边界、独立 49,999/50,000 provider 边界 fixture、午夜/rolling 24-hour、
分页、失败、断电式恢复测试，以及一个 `--max-items 1` 真实 schema pilot。

完成条件：完整测试套件和 lint 通过；pilot 的每次 socket attempt、raw hash、耗时和状态转换
可审计，随后离线 `sync`/`verify` 成功。M7 完成只表示 CLI 可安全使用，不表示历史数据已下载、
M4 已关闭或 M6 可以启动。

稳定 JSON、预算/锁/恢复、分页/失败/断电式 fixture、操作手册、ARM64 代码验收和真实目标盘
pilot 均已完成。异卷备份验证保留为 M8 bulk wave 的前置门禁。

## 后续实际下载顺序

M7 CLI 生成的 manifest 仍按以下顺序执行，但完整执行属于 M8：低频 instrument/calendar/index
membership，批量不复权日线与复权因子，provider `adjustflag=2`，有界 09:35 五分钟数据，最后
corporate action。具体 API、切片和消费职责见 [`baostock-data-plan.md`](baostock-data-plan.md)。

## 每个 fetch session 的固定闭环

```text
人工确认 frozen manifest hash 和 max-items
  -> 唤醒目标目录并通过 write/fsync/lock/space preflight
  -> 获取 data-root lock，排除同盘离线写入与恢复
  -> 获取 global provider lock
  -> 检查自然日/rolling 24h/session/item 预算并预留 logout
  -> login，逐次记录 socket attempt
  -> 对有界 item 严格顺序 query/有限分页/raw fsync/checkpoint
  -> 任一 item 失败即停止队列
  -> 正常路径 logout；若命中 10001011 则只在本地关闭 socket
  -> 落盘 session 终态后释放 global lock
  -> 恢复 offline mode，运行 sync/verify
  -> 校验 raw hash、exact-query index、schema、冲突、覆盖和 as_of
  -> 固定 content-addressed verify report 和首次 completion receipt
  -> bulk wave 会话结束时按另行冻结的方案执行异卷备份和 hash 复验
```

旧 `ParquetDataStore.merge()` 的覆盖更新行为不用于 bulk backfill；M7 `sync` 已把相同业务主键的
不同历史值送入 quarantine 并保留两个 raw hash，等待人工确认 provider 修订语义后再处理。

## 停止条件

遇到以下任一情况立即停止当日所有 live fetch，不自动重试：

- 任一响应出现 `error_code="10001011"`；不再发送 logout，记录公网 IP 和审计状态，由操作者
  在 BaoStock QQ 群联系管理员解决，在确认解除并追加人工恢复事件前禁止任何后续 live fetch；
- login、query、pagination 或 logout 返回非零 provider code、timeout、断连或 malformed payload；
- 字段集合、字段顺序、类型、日期/代码语义或 `adjustflag` 与已批准 contract 不同；
- 实际页数或 socket 数超过 manifest 预算；
- 重复主键、同一业务键历史值冲突、OHLC/复权因子不一致或预期覆盖异常；
- raw/hash/index 校验失败，或 normalization/quality 未通过；
- 目标目录不可写、锁或 request ledger 损坏、系统时钟回退；
- 可用空间低于 `100 GiB`、占用率达到 `90%`，或自然日、rolling 24-hour、session、单 item
  任一预算不足；
- 已有 global lock、同 IP 存在其他 BaoStock 客户端，或项目计数可能遗漏外部访问；
- 操作波次要求的上一个 item 尚未完成 offline 验证，或 bulk wave 的上一会话尚未完成异卷
  备份。M7 v1 不自动执行复制，因此在 backup target 冻结前不得启动 bulk wave。

除 `10001011` 外，provider 明确错误和 schema 漂移需要人工分析后生成新 manifest；网络类失败
最早也应等持久化 cooldown 结束并在下一次人工批准后重试。黑名单状态只能在管理员确认解除后
通过追加式人工恢复事件开放新会话。任何情况都不能删除失败 attempt 来恢复预算。

## 已实现命令 contract

M7 已注册独立 entrypoint。除 `fetch` 外，以下命令均为离线操作；示例保留完整标准流程。
首个 live pilot 已按该流程完成；M8 的每个新 manifest 仍须先审阅 spec 和 frozen hash，并逐项
获得明确批准：

```bash
BAOSTOCK_DATA_ROOT=/mnt/exos_1t/quant/baostock

uv run trading-codex-baostock \
  --data-root "$BAOSTOCK_DATA_ROOT" \
  doctor --initialize --json

uv run trading-codex-baostock \
  --data-root "$BAOSTOCK_DATA_ROOT" \
  plan create --spec backfill-spec.json

uv run trading-codex-baostock \
  --data-root "$BAOSTOCK_DATA_ROOT" \
  plan freeze --manifest "$BAOSTOCK_DATA_ROOT/manifests/draft/<manifest-id>.json"

uv run trading-codex-baostock \
  --data-root "$BAOSTOCK_DATA_ROOT" \
  status \
  --manifest "$BAOSTOCK_DATA_ROOT/manifests/frozen/<manifest-id>.json" \
  --json

uv run trading-codex-baostock \
  --data-root "$BAOSTOCK_DATA_ROOT" \
  fetch \
  --manifest "$BAOSTOCK_DATA_ROOT/manifests/frozen/<manifest-id>.json" \
  --confirm-manifest-sha256 <sha256> \
  --max-items 1

uv run trading-codex-baostock \
  --data-root "$BAOSTOCK_DATA_ROOT" \
  sync --manifest "$BAOSTOCK_DATA_ROOT/manifests/frozen/<manifest-id>.json"

uv run trading-codex-baostock \
  --data-root "$BAOSTOCK_DATA_ROOT" \
  verify \
  --manifest "$BAOSTOCK_DATA_ROOT/manifests/frozen/<manifest-id>.json" \
  --as-of 2026-08-09T15:00:00+08:00 \
  --json
```

只有 `fetch` 可以加载 BaoStock 网络 adapter。`doctor`、`plan`、`status`、`sync` 和 `verify`
在代码结构上保持零网络。`trading-codex-data sync --fetch-missing` 因不满足 provider 限制已
永久禁用联网能力，避免出现第二个绕过 global lock 的入口。

## 备份边界

- bulk wave 开始前必须指定另一块物理卷作为 backup target；同一 `/mnt/exos_1t` 内复制不算备份。
- raw、`state/`、frozen/completed manifest 和 quarantine failure record 属于必须备份的数据；
  normalized、报告和 M8 OOS artifact 在每个完成波次后备份。
- 备份 manifest 记录源相对路径、大小、SHA-256、目标卷身份、复制和复验时间。未完成 hash 复验
  的 copy 不得解除下一波次门禁。
- 备份只新增；删除或压缩 raw 需要独立 retention 决策，不包含在本计划内。
- 三块 EXOS 盘均受 300 秒 `hd-idle` 控制；备份按会话集中执行并只唤醒目标卷，不为逐 item
  复制而把多块盘分散唤醒一整天。

## M7 完成与停止边界

- M7 只交付并验证 `trading-codex-baostock`；不会以下载行数、日期覆盖或回测表现作为完成条件。
- 除最终单项 schema pilot 外，M7 开发和测试不访问 BaoStock。pilot 未获用户明确批准时，
  只能达到 `code_complete/live_pilot_pending`，M7 仍保持未关闭。
- M7 完成后，主应用、回测和 `trading-codex-data` 仍不得访问 BaoStock 网络；只有独立 CLI 的
  `fetch` 子命令有此权限。
- 完整 manifest 下载、数据质量闭环和真实 OOS 属于 Proposed M8。M8 证据不足时 M4 继续开放，
  M6 timer 和 live AI proposal 继续禁止。

## M8 运行前仍需确定的参数

1. 代码已接受项目默认 `2,000` attempts/day、rolling 24-hour `2,000`、项目硬上限 `45,000`、
   最小间隔 `3` 秒、session 最多 `100` attempts 和命令默认 `1` item；首个 pilot 已把最小
   间隔提高到 30 秒，其余 endpoint 的 schema pilot 继续使用 30 秒，除非另行审阅并冻结。
2. global provider state 已固定在 `$XDG_STATE_HOME/trading-codex/baostock`，未设置 XDG 时使用
   `~/.local/state/trading-codex/baostock`；实际运行环境不能通过切换 XDG 路径分裂计数。
3. 独立 backup target 仍未确定。单项 schema pilot 可先验证字节规模，但任何 bulk wave 前必须
   冻结异卷目标和复验流程。
4. M8 的完整历史日期、预热、train/validation/test 边界和候选生成规则仍未冻结。
5. 其余 endpoint 的 schema pilot 完成后需要重算空间上界，再决定是否缩短历史或分钟线范围。

M7.0-M7.5 已完成。manifest `bs-af5dfdaa19fc5c6ae075` 以 4 次成功 attempt 获取
`sh.600000` 的 3,644 行前复权日线，离线 sync/verify 和 completion receipt 均通过；该证据不
外推到其他 endpoint 或完整 universe。
