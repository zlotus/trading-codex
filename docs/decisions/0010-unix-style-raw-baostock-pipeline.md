# ADR-0010：使用 raw-only、存在即跳过的 Unix 风格 BaoStock 工具链

- 状态：Accepted
- 日期：2026-08-10
- 取代：ADR-0009
- 被取代：无

## 背景

ADR-0008/0009 建立的 M7 下载器把 exact-query manifest、计划冻结、SHA 人工确认、存储空间
预检、请求状态机、raw 下载、Parquet 发布、逐行验证和备份前置条件组织成一个操作流程。真实
pilot 证明 provider adapter、逐 socket 计数和 raw 落盘可行，但正常下载一项数据需要依次执行
`doctor`、`plan create`、`plan freeze`、`status`、`fetch`、`sync` 和 `verify`，心智负担与
“阻塞式顺序下载到指定目录”的需求不相称。

BaoStock 官方 blacklist 页面只明确两条访问限制：每日 API 请求不得超过 50,000 次、禁止并发
连接。下载范围应由 Trading Codex 根据研究需求生成，下载器不应决定 universe、训练/测试边界、
备份策略或 normalized schema。

## 决策

1. `trading-codex-requirements` 把 Trading Codex 的数据需求输出为 JSONL exact request；
   `trading-codex-baostock` 只读取该请求流，使用一个 login session 阻塞、顺序执行，不提供
   worker、并发、timer 或自动重试。
2. `--data-root` 继续直接决定落盘目录。每个请求使用 provider、client version、operation 和
   query 的 SHA-256 生成固定目标路径：
   `raw/baostock/<operation>/<request-id>.json`。目标是普通文件就直接跳过；下载器不会因为
   文件内容异常自动重下、覆盖或删除。
3. raw 使用版本化 envelope：canonical JSON 保存 exact query、字段、原始字符串行、接收时间和
   payload SHA-256；`envelope_sha256` 再覆盖上述内容、payload hash 和接收时间。下载器先验证
   待写 bytes，再使用同文件系统临时文件、`fsync` 和 atomic replace 发布，最后从磁盘重新读取
   刚写文件并验证 envelope。这个自检结果不向下游传递信任。
4. 下载器只保留访问限制所需的最小持久事实：本机 XDG state root 的全局非阻塞 `flock`，以及
   `attempts/YYYY-MM-DD.jsonl` 文本计数。每次底层 socket send 在发送前追加一行；自然日按
   `Asia/Shanghai` 计算。官方上限保持 50,000，程序默认在 40,000 次停止，并为 logout 保留
   最后一次；不再叠加 rolling 24-hour、session、item 或最小时间间隔预算。
5. `10001011` 仍立即停止并写一个简单 blacklist marker；确认 BaoStock 已解除后才能人工删除。
   普通 provider、写盘或 envelope 自检错误在第一处立即退出，不自动重试。重新运行同一请求流
   时，已经存在的目标文件被跳过，其余请求继续执行。
6. 公开下载 CLI 不再暴露 `doctor`、`plan`、`status`、`fetch`、`sync`、`verify`、`import-raw`
   或 `recover`，也不做空间估算、容量阈值、mount 探针、研究分区冻结或备份门禁。目录创建和
   真实写入失败直接作为文件系统错误返回。
7. `trading-codex-data inspect-raw` 独立读取每个 envelope，重新验证 canonical JSON、payload
   hash、envelope hash、固定文件地址、endpoint 字段和行结构；`ingest-raw` 再独立验证并按 raw
   payload hash 幂等发布 normalized Parquet segment。跨 payload 业务键冲突拒绝发布。坏 raw
   只报告 warning，不触发网络行为。质量、覆盖、回测和备份继续由各自工具处理。
8. ADR-0008/0009 实现的 manifest/SQLite/offline 模块暂时保留为已有 pilot 的兼容读取路径，
   但不属于正常下载流程，也不能由主应用、回测或 scheduler 调用联网。

## 理由

下载器能够可靠知道的只有请求、socket 发送、provider 响应和目标文件写入。把研究边界、磁盘
容量预测、备份和数据质量放进下载门禁，既不能提高 provider 数据真实性，也让一个可重复命令
变成需要人工维护的状态机。

query-addressed 文件让幂等规则等价于普通文件存在性测试；下载失败时不会出现最终目标文件，成功
后重复执行不产生请求。envelope 让下载端和预处理端可以针对同一协议各自验证，而不是让下游信任
上游的完成状态。纯文本 attempt 日志足以执行 40,000 次日边界，不需要 SQLite session 状态。

## 考虑过的方案

- 继续保留 manifest 但自动执行全部阶段：拒绝，因为隐藏状态机仍然存在，下载范围和数据处理
  职责仍与网络工具耦合。
- 下载器启动时校验所有既有 raw，坏文件自动重下：拒绝，因为本地损坏会隐式消耗新的 provider
  请求，也破坏“存在即跳过”的可预测幂等语义。
- 完全不保存请求计数：拒绝，因为进程重启会绕过 BaoStock 每日上限。
- 只按高层 API 调用计数：拒绝，因为 login、pagination 和 logout 都会执行底层 socket send，
  provider 未公开其计数口径。
- 在下载前预测完整数据大小：拒绝，因为响应行数和压缩后大小未知；真实写盘失败足以停止命令。

## 后果

- 操作者只需准备或管道输入 JSONL；重复执行相同命令就是断点续传。
- 文件存在不代表文件有效。`inspect-raw`、`ingest-raw` 和后续 quality gate 必须各自验证输入；
  M4-M6 仍在缺失、损坏、不一致或 future data 时 fail closed。
- 下载器无法统计同一公网 IP 上其他机器或独立脚本的请求。40,000 次余量降低风险但不能替代
  出口协调；运行期间仍不得启动第二个 BaoStock 客户端。
- 固定沪深300和中证500当前快照的 15 年双价格日线可用于真实数据 smoke test、性能测量和演示，
  但存在幸存者偏差，不能作为关闭 M4 正式 OOS 验收的证据。
- 备份是 raw 文件完成后的独立运维动作；没有 backup target 不再阻止下载。
