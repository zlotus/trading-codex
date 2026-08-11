# ADR-0008：使用清单驱动且全局串行的独立 BaoStock 下载边界

- 状态：Superseded by ADR-0009
- 日期：2026-08-09
- 取代：无
- 被取代：ADR-0009

## 背景

Milestone 4 的真实 OOS 验收需要补齐双价格日线、历史 universe、09:35 bar 和 corporate
action。BaoStock 官方 blacklist 规则要求同一公网 IP 每日 API 请求不超过 50,000 次并禁止
并发连接，命中 `10001011` 后必须联系管理员人工处理。官方没有定义自然日时区、高层 API 与
底层 socket 的计数关系、失败请求口径或最小请求间隔。

原有 `trading-codex-data --fetch-missing` 只能限制单进程 Python loader 次数，不能跨进程锁定
provider，也不能逐次记录 login、query、pagination 和 logout。把长期回填放进主应用、回测
或 scheduler 会产生第二条联网路径，也会让重启、并发或 data root 变化绕过预算。

## 决策

1. 使用独立命令 `trading-codex-baostock` 承担 BaoStock 下载。只有 `fetch` 子命令可以加载
   provider adapter；`doctor`、`plan`、`status`、`sync`、`verify`、`import-raw` 和
   `recover` 保持离线。旧 `trading-codex-data --fetch-missing` 永久阻断联网。
2. `fetch` 只接受内容 hash 已由操作者确认的 frozen exact-query manifest。manifest 固定
   provider/client 版本、官方规则资源 SHA-256、endpoint、完整参数、字段顺序、分页、依赖、
   空间估算和请求预算；draft 或内容不一致时 fail closed。
3. 在本机唯一 XDG state root 使用跨进程 `flock` 和 append-only SQLite 请求账本。每次底层
   `send_msg` 必须先原子预占 attempt，再发送；login、query、每页、logout、失败和崩溃均
   消耗预算。外置盘 state 只是 SQLite online backup 审计镜像，不是锁或预算事实来源。
4. 同时执行 `Asia/Shanghai` 自然日和 rolling 24-hour 预算。provider 50,000 次硬上限、项目
   45,000 次硬上限及 3 秒最小间隔不可上调；默认两类时间预算各 2,000 次、单 session 100
   attempts、单次命令 1 item。程序不提供 worker、thread、daemon、timer 或自动重试。
5. 每次联网前验证 mount identity、filesystem UUID、可写性、`fsync`、atomic replace、
   `flock` 和空间边界。同一 data root 的 fetch、离线发布、raw import 和恢复共用排他锁。
   raw 内容寻址且只新增；normalized 数据只从本地 raw 生成 immutable manifest segment，
   schema 漂移、重复键和历史值冲突进入 quarantine；验证报告和首次完成回执不可覆盖。
6. 任一 socket 响应出现 `10001011` 时，立即持久化 blacklist incident、本地关闭 socket 且
   不发送 logout。跨日、cooldown 或更换 manifest 都不能解除；只有管理员确认后追加人工恢复
   事件才能开放新 session，恢复不能删除 attempt 或重置预算。
7. 真实 pilot 必须再次获得操作者明确批准，使用 `--max-items 1` 和至少 30 秒间隔。代码和
   fake-socket 验收不等于已经访问 provider、写入外置盘、补齐数据或关闭 M4。

## 理由

把联网能力收敛到一个一次性进程，可以在不引入服务端调度器或分布式锁的情况下，明确证明
同一主机只有一条受预算约束的 socket 路径。全局本地 state 不依赖外置盘是否休眠，也不会因
更换 data root 分裂计数；外置盘仍保存可恢复的 raw 和审计镜像。冻结 manifest 把“准备下载
什么”和“执行请求”分开，使日期、字段与 OOS 边界不能在看到结果后悄悄改变。

逐 socket append-before-send 采用最保守计数，宁可低估可用额度，也不依赖 BaoStock 未公开的
计数细节。immutable raw 加离线 segment 让网络故障、normalizer 漂移和回测失败彼此隔离。

## 考虑过的方案

- 继续扩展 `--fetch-missing`：拒绝，因为 loader 次数不等于 socket 次数，且没有全局锁、持久
  cooldown、rolling 24-hour 预算或黑名单恢复状态。
- 在 FastAPI worker 或 M6 scheduler 内回填：拒绝，因为 worker 数量和重启会改变并发与请求
  生命周期，也会把研究数据缺口变成隐式线上副作用。
- 直接按官方 50,000 次上限运行：拒绝，因为同一公网 IP 的其他流量不可见，且官方没有说明
  失败、分页和跨午夜的计数细节。
- 多 worker 并行不同证券或日期：拒绝，因为官方明确禁止并发连接。
- 命中错误后自动退避重试：拒绝，因为失败仍可能计数，`10001011` 明确要求人工联系管理员。
- 每次重写单文件 Parquet：拒绝，因为长期回填会导致无界读写放大和中断风险。

## 后果

- global state 是联网门禁的一部分；缺失、损坏、规则 hash 变化、时钟回退或未关闭 session 时
  必须先人工审计恢复，不能联网。
- CLI 只能看见自身 attempt，无法发现同一 NAT 下其他主机或独立脚本。操作规程必须在 fetch
  期间禁止其他 BaoStock 客户端，无法协调时应停止或进一步下调预算。
- `/mnt/exos_1t/quant/baostock` 的真实 NTFS/FUSE 行为、空间和 mount identity 仍要由首次
  `doctor --initialize` 验证；fake filesystem 测试不能替代该证据。
- M7 不负责完整回填、异卷复制实现或 OOS 结论。批量执行前仍需确定 backup target，并在 M8
  冻结历史范围、预热及 train/validation/test 边界。
- BaoStock 官方规则资源 hash 变化时，现有 state 与 manifest 会 fail closed；必须人工复核规则
  并通过新的 ADR 或 contract 变更处理，不能自动采用更宽松解释。
