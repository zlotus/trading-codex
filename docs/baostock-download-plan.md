# Milestone 7：Unix 风格 BaoStock raw 下载器

- 状态：`completed`
- data root：`/mnt/exos_1t/quant/baostock`
- 下载命令：`trading-codex-baostock`
- 请求生成命令：`trading-codex-requirements`
- 离线数据命令：`trading-codex-data`
- 计划版本：`baostock-download-plan-v4`

M7 的最终边界由 ADR-0010 定义：下载器只消费 exact-request JSONL、串行访问 BaoStock、保存
raw envelope。请求生成、raw 检查、规范化、质量评估、回测和备份分别由独立工具完成。

## 已交付

### M7.0：provider contract

- BaoStock `00.9.30` endpoint adapter 和固定字段 contract；
- 日线完整日期范围、指数成分、交易日历、证券基础、复权因子、分钟线和 dividend operation；
- `10001011` 识别和本地 socket 关闭。

### M7.1：最小访问门禁

- 跨 data root 的单用户全局非阻塞 `flock`；
- 每个底层 socket send 发送前追加一行自然日 JSONL 计数；
- 官方 50,000 次上限，项目默认 40,000 次停止；
- 一个 login session 内严格顺序，无 worker、并发或自动重试；
- 兼容计入旧 M7 SQLite 中同日已经发生的 attempt。

### M7.2：query-addressed raw

- exact request 决定固定文件名；文件存在直接跳过；
- canonical JSON envelope 保存 exact query、provider 原始字段/行、接收时间、payload SHA-256 和
  覆盖全部元数据的 envelope SHA-256；
- 临时文件、`fsync`、atomic replace；
- 新文件落盘后重新读取自检；首错立即停止。

### M7.3：独立离线处理

- `trading-codex-data inspect-raw` 对每个文件独立复验 envelope，不访问网络；
- `trading-codex-data ingest-raw` 再次复验并按 payload hash 幂等发布 normalized segment；
- 坏 raw 输出 warning，不触发下载或覆盖；
- 后续 quality、coverage 和回测继续执行自己的 fail-closed contract。

### M7.4：Trading Codex 请求生成

- `index-memberships` 生成沪深300和中证500快照请求；
- `base-daily` 从 normalized 沪深300/中证500成分并集生成 15 年双价格日线请求；
- 默认基础集包含 800 个成分、instruments 和 trade calendar；
- JSONL 可保存为文件或直接管道输入下载器，下载器不决定 universe。

### M7.5：兼容与迁移

- 旧内容寻址 raw、manifest segment 和 2026-08-10 pilot 保持可读；
- 旧 manifest/SQLite/offline 模块暂留包内，但不再由公开下载 entrypoint 暴露；
- 不迁移、覆盖或删除 `/mnt/exos_1t/quant/baostock` 现有文件。

## 幂等契约

```text
Trading Codex requirement
  -> JSONL exact request
  -> target raw file exists? ---- yes -> skip
             |
             no
             v
      serial BaoStock call
             |
             v
      atomic raw envelope + downloader self-check
             |
             v
      independent inspect / ingest / quality / backtest
```

下载器不把“existing”解释为“valid”。它只保证同一 exact request 不会因为重复运行而再次访问
provider。离线工具也不相信下载器的自检结果，每次都从磁盘重新验证 envelope。

## 不再属于下载门禁

- `doctor` 和目录初始化步骤；
- draft/frozen manifest、人工 SHA 确认、status 和 recover 状态机；
- estimated peak bytes、剩余空间阈值、mount/UUID/设备识别；
- normalized sync、逐行 verify、quarantine 和 completion receipt；
- train/validation/test 边界冻结；
- 异卷 backup target。

这些能力中仍有价值的部分归属其他工具。实际写盘失败仍停止；研究数据缺失或不一致仍由数据消费
层 fail closed；备份仍应执行，但发生在 raw 文件完成之后。

## 已有真实证据

2026-08-10 在 `/mnt/exos_1t/quant/baostock` 完成：

- `sh.600000` 2011-2025 前复权日线 3,644 行；
- `sh.600000` 同范围不复权日线 3,644 行；
- 2024-06-07 查询的沪深300成分 300 行；
- 同日中证500成分 500 行，与沪深300交集为 0；
- raw、旧 normalized segment 和旧 verify 均通过，当日累计 14 次 socket attempt。

这些是 provider/schema pilot，不是 800 个标的完整回填或 M4 OOS 结论。

## 验收

M7 简化入口至少覆盖：

- 两个进程只有一个可以进入下载区；
- 请求严格按 JSONL 顺序执行；
- 目标文件存在时为零网络；
- 中途 provider 错误后重跑只补缺失文件；
- 下载器不创建 normalized、manifest、report 或 backup；
- 不做空间预测，真实写入错误立即退出；
- 日计数持久化、达到边界暂停、次日续传；
- `10001011` 停止且不自动重试；
- 下载端自检和预处理端复验均能发现 envelope 损坏；
- ingest 重跑不重复发布 segment，跨 payload 业务键冲突不会进入 normalized。

## 后续里程碑

### M8.0：默认基础数据回填（已完成）

2026-08-11 已完成固定 2024-06-07 成分的 1,602 条请求、全部缺失 raw 下载、独立
`inspect-raw`、干净 `ingest-raw` 和逐 segment 验收。800 个标的双轨日线共 4,973,298 行；
完整读取耗时 16:43.49、峰值 RSS 约 12.3 GiB。该结果只关闭 M8.0，不关闭正式 M4 OOS。

### M8.1：M4 真实数据 smoke（已完成）

为固定 universe 增加明确标记为 survivorship-biased 的 EOD smoke runner，验证 M4 决策管线、
成本、RQAlpha adapter 和 walk-forward 计算能在真实规模运行。2026-08-11 已完成 800 标的、
528 个交易日、默认 252/63 和两组参数的完整运行；峰值 RSS 约 1.17 GiB，artifact hash 与代码
provenance 已独立校验。该报告显式为 `formal_m4_oos=false`，不能关闭 M4。

### M8.2：point-in-time universe 与 benchmark（已完成）

2026-08-12 已完成 3,789 个交易日的逐日指数成分、historical universe、中证800 benchmark 和
全部曾任成员双价格。覆盖门禁在按 `[ipo_date, out_date)` 排除 41 个 provider 退市边界残留后，
验证 3,031,137 个有效成员日三轨完整，全部 issue count 为 0。22 个真实 499-member 中证500日
原样保留。该结果仅关闭 M8.2；corporate action、经批准范围内的 09:35 数据和 untouched OOS
artifact 仍属于 M8.3/M8.4。

M5 的 live LLM key/provider 继续暂缓。M6 的 60 个真实前瞻 observation 不能由历史下载替代，
但 M8 可先解除其底层真实数据分析阻塞。
