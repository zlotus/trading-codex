# ADR-0009：由 data root 直接决定 BaoStock 落盘位置

- 状态：Accepted
- 日期：2026-08-09
- 取代：ADR-0008
- 被取代：无

## 背景

ADR-0008 建立了 manifest 驱动、全局串行、逐 socket attempt 记账的独立 BaoStock 下载
边界，同时要求操作者额外提供 expected mount，并由程序冻结 mount point、filesystem 和 UUID
身份。实际使用证明这层身份检查把简单的目录选择变成了设备识别流程，也会因为容器或沙箱看不见
宿主机块设备而误阻断一个已经可写的目录。

下载器真正需要保证的是目标目录当前可写、写入语义可靠、空间足够，以及多个命令不能交叠。
它不需要判断路径位于系统盘、数据盘、外置盘还是某个特定 UUID；存储位置由操作者明确选择。

## 决策

1. `--data-root` 是唯一的落盘位置参数。传入哪个目录，程序就在该目录创建布局并读写 raw、
   normalized、manifest、report、quarantine 和 data-root state。默认值仍为
   `/mnt/exos_1t/quant/baostock`，也可以通过 `BAOSTOCK_DATA_ROOT` 或命令行覆盖。
2. 删除 `--expected-mount`、`BAOSTOCK_EXPECTED_MOUNT`、filesystem UUID、mount identity
   记录及其变化门禁。程序不区分系统盘、数据盘、外置盘，也不检查该目录是否恰好是 mount point。
3. `doctor --initialize` 递归创建 data root 和固定目录布局。每次可能读写数据的操作仍检查目录
   是否存在；`fetch` 在 login 前仍验证实际 write、`fsync`、atomic replace、`flock` 和空间边界。
4. 同一 data root 的排他锁、内容寻址 immutable raw、immutable Parquet segment、quarantine、
   completion receipt 和离线重放验证保持不变。旧版本留下的 `state/storage-identity.json` 不再
   参与判断，也不需要删除。
5. ADR-0008 的独立联网入口、frozen manifest、XDG global state、逐 socket attempt 记账、
   双时间预算、禁止并发与自动重试、`10001011` 硬停止和单项 pilot 规则继续有效。

## 理由

路径是操作者已经明确给出的事实。下载器再推断设备类别或身份既不能证明这是期望的业务盘，
也不能发现同一公网 IP 上的其他 BaoStock 客户端，却会引入依赖 `/proc`、`/dev/disk/by-uuid`
和宿主机 mount namespace 的误报。

保留实际 I/O 探针和空间检查，可以在不绑定设备身份的情况下发现只读路径、不支持的原子写入、
失效的文件锁和空间不足；这些才是下载前与数据完整性直接相关的条件。

## 考虑过的方案

- 保留 expected mount 但允许跳过 UUID：拒绝，因为仍有第二个位置参数和 mount namespace 依赖。
- 自动寻找 data root 所在的最近 mount point：拒绝，因为结果只适合诊断，不应决定是否允许下载。
- 删除全部存储 preflight：拒绝，因为只读目录、空间不足或不可靠的锁会直接导致半写数据或并发
  发布，和设备分类不是同一问题。

## 后果

- 操作者应确认 `--data-root` 拼写正确；程序不会阻止把数据写到任意可写文件系统。
- 未挂载的目标路径如果仍然可写，程序不会尝试猜测操作者原意。需要这种运维保证时，应由宿主机
  的 mount/systemd 配置负责，而不是写入 BaoStock 下载协议。
- 当前 Codex 沙箱仍可能把工作区外目录映射为只读；这是执行环境权限，不再被误报为 UUID 或
  mount identity 问题。
