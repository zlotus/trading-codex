# 前瞻模拟运维

Milestone 6 的运维 contract 已实现，但前瞻运行尚未启用。M4 真实 OOS 验收、daily task
composition、实时行情 adapter 和模型 adapter 仍未完成，当前真实观察为 **0/60 个交易日**。
本指南不能作为安装 timer 或开始模拟交易的授权。

## 运行边界

```text
外部 timer
  -> 当前交易日与时间窗检查
  -> critical provider health gate
  -> append-only job attempt lease
  -> EOD 或 09:35 task
  -> observation / alert / backup evidence
```

- `OneShotDailyScheduler` 不驻留在 FastAPI 内；systemd timer 或同等外部机制负责重复唤醒。
- 默认窗口为 Asia/Shanghai 09:35 和 15:30 起的 20 分钟，只运行当前交易日，不自动回补。
- 同一个 schedule 使用稳定 run key。30 分钟内已有 active attempt 时，第二个进程只读取
  `running`，不会再次调用 task；超时 attempt 先追加 `failed`，再允许新 attempt。
- calendar 缺失、critical provider 非 `healthy`、task 未配置或 task 抛错时均 fail closed。
  health 失败发生在 task 调用前，因此不会产生该 attempt 的组合事件。
- optional AI provider 的 `not_configured` 不阻断 base task；它也不会把 AI 状态提升为 ready。

当前尚无可安装的 systemd unit，因为仓库还没有把真实 EOD、09:35 数据快照、模拟成交和
provider adapters 组合成 daily task。现在安装空 timer 只会制造误导性运行记录。

## 状态与告警

ledger schema v4 新增三类 append-only 证据：

| 表 | 内容 |
| --- | --- |
| `provider_health_checks` | provider、critical 标记、状态、latency、detail 和 metadata |
| `alert_events` | provider、calendar 或 job 的 `opened` / `resolved` 转换 |
| `forward_observations` | 每日收益、成本与完整 decision/snapshot/hash 轨迹 |

只读 API：

```bash
curl http://127.0.0.1:8000/api/v1/operations/status
curl http://127.0.0.1:8000/api/v1/operations/observations
```

`/operations/status` 返回 scheduler 模式、`scheduler_activated=false`、最新 provider
health、15 分钟 freshness health gate、未恢复告警、观察日数和 60 日报告门槛。系统总状态中的
`operations` 目前固定为 `not_configured`；health contract 通过合成测试不等于真实 provider
或 timer 已上线。

当前没有远程 notification adapter。告警保存在账本，可通过只读 API、CLI 非零退出码和
进程日志观察；在验证实际通知送达前，不能声称告警链路完成。

## 一致性备份与 replay

备份目录应位于仓库外，并由操作者设置与账本同等或更严格的文件权限。创建备份：

```bash
trading-codex-ops backup \
  --ledger-path data/trading-codex.db \
  --destination /srv/trading-codex/backups
```

命令使用 SQLite online backup 读取包含 WAL 的一致视图，生成 `.db` 与相邻的
`.manifest.json`。文件名包含时间和数据库 SHA-256；已有不同内容不会被覆盖。

校验与 replay：

```bash
trading-codex-ops verify-backup \
  /srv/trading-codex/backups/ledger-<timestamp>-<hash>.manifest.json

trading-codex-ops replay-backup \
  /srv/trading-codex/backups/ledger-<timestamp>-<hash>.manifest.json
```

`verify-backup` 检查 manifest、文件 SHA-256、逻辑内容 hash、table counts、SQLite
`quick_check`、foreign keys 和 append-only triggers。`replay-backup` 先完成同样校验，再把
数据库复制到临时目录，在副本上执行 schema migration 和三轨投影；原备份保持不变。

备份存在不等于恢复完成。启用 M6 前至少应人工完成一次“创建、校验、replay、核对三轨
cash/equity/position counts”的闭环，并记录实际备份位置和保留策略，但不得提交数据库或
manifest 到 Git。

## 前瞻观察与归因

`forward_observations` 没有 HTTP 写入口，只能由内部 daily composition 在指标 artifact
落盘后追加。每个交易日只能有一条 observation，必须包含：

- 同一 snapshot 的 base 与 AI-shadow decision ID；
- base 和 AI-shadow configuration ID；
- market-data source payload SHA-256 与独立 metric payload SHA-256；
- benchmark、无摩擦 base target、base 模拟、AI-shadow、actual 日收益和成本率。

生成报告：

```bash
trading-codex-ops review \
  --ledger-path data/trading-codex.db \
  --output-directory artifacts/forward-review
```

少于 60 个唯一交易日时，命令输出 `status=blocked` 并以退出码 2 结束，不创建报告。达到门槛
后才分别报告：

- `strategy_vs_benchmark`：无摩擦 base target 相对 benchmark；
- `simulated_execution_vs_target`：base 模拟相对无摩擦 base target；
- `ai_overlay_vs_base_simulation`：AI-shadow 相对 base 模拟；
- `manual_execution_vs_base_simulation`：actual 相对 base 模拟。

最后一项是平行归因，不与前三项强行相加。报告保留全部 observation、decision、snapshot、
metric 和 source payload hash，便于逐日 replay。

## 启用前检查

1. 先关闭 M4：补齐预先划分的真实数据并通过扣费 OOS 与敏感性审阅。
2. 接入实时行情与必要 provider probes，明确哪些是 critical；BaoStock 只允许通过 M7 frozen
   manifest CLI 人工、全局串行补历史数据，不能充当 M6 实时行情 provider。
3. 组合真实 EOD 与 09:35 task，证明所有数据查询使用显式 `as_of`，并在测试账本完成中断、
   并发、lease 超时和 retry 演练。
4. 先创建并 replay 一份生产账本备份，再安装外部 timer；从只写 base / AI-shadow 的受控
   dry-run 开始。
5. 验证远程告警实际送达并记录恢复流程。未验证前只能依赖本地账本、退出码与日志。
6. 连续收集真实 observation。未满 60 个交易日不得生成或展示通过状态，也不得扩大 AI 权限。
