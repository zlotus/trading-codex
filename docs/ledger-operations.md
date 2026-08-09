# 组合账本操作指南

Milestone 3 使用本地 SQLite append-only 事件账本。默认文件为
`data/trading-codex.db`，可通过环境变量修改：

```bash
TRADING_CODEX_LEDGER_PATH=/srv/trading-codex/ledger.db
```

数据库文件、WAL 和共享内存文件均属于本地运行状态，不得提交到 Git。

## 三条组合轨道

| track | 来源 | HTTP 写权限 |
| --- | --- | --- |
| `base` | 确定性决策与模拟执行 | 无；只允许内部流程写入 |
| `ai_shadow` | 通过边界校验的 AI 影子提案与模拟执行 | 无；只允许内部流程写入 |
| `actual` | 人工录入的真实现金和成交 | 允许人工 API 写入 |

AI workflow 不能调用 actual 成交接口，也不能把文本建议直接转换成 fill。

## 初始现金

新账本从零现金开始。录入第一笔买入成交前，必须先追加 actual deposit；否则账本会因
负现金 fail closed。

```bash
curl -X POST http://127.0.0.1:8000/api/v1/ledger/cash-movements \
  -H 'Content-Type: application/json' \
  -d '{
    "portfolio_track": "actual",
    "kind": "deposit",
    "amount": "100000.00",
    "occurred_at": "2026-08-08T01:00:00Z",
    "idempotency_key": "opening-cash-20260808",
    "note": "初始资金"
  }'
```

同一个 `idempotency_key` 可以用完全相同的 payload 安全重试；改变金额、时间或其他字段
会返回 `409 Conflict`。

## 查询与人工执行

- `GET /api/v1/ledger/dashboard`：按当前时点返回三轨现金、持仓、信号和 reconciliation。
- `GET /api/v1/ledger/dashboard?as_of=<timestamp>`：重建指定时点之前的账本状态。
- `GET /api/v1/ledger/signals/{signal_id}`：返回信号、双价格历史和 decision/snapshot/source
  追溯信息。
- `POST /api/v1/ledger/fills`：追加人工 actual fill，并在同一事务中追加 trade 与 fee cash
  movement。
- `POST /api/v1/ledger/signals/{signal_id}/skip`：保留已成交数量并明确跳过剩余信号。
- `GET /api/v1/ledger/jobs`：查看 EOD preparation 和 09:35 decision 的重试记录。
- `GET /api/v1/ai/workspace`：按显式 `as_of` 返回最新 AI 摘要、提案、验证结果、usage、
  assistant 消息和 base/AI-shadow target 对比；该路由只读。

Web 的“今日决策”页面提供信号详情、部分成交回填和跳过操作；“组合仓位”页面显示
base、AI-shadow、actual 三轨权益和数量偏差。

## 不变量与当前限制

- event table 的 `UPDATE` 和 `DELETE` 会被 SQLite trigger 拒绝。
- ledger schema v4 的 `ai_runs`、`ai_messages`、`provider_health_checks`、`alert_events` 和
  `forward_observations` 同样只允许追加。AI 事件关联 base/AI-shadow decision、prompt、
  provider/model 和 cache/usage；前瞻 observation 关联双轨 decision、snapshot 和 metric/
  source payload hashes。
- 每次影响现金或持仓的写入后都会重放受影响 track；负现金、累计成交超过 intent、
  T+1 超卖或成交晚于 intent 失效时间都会回滚整次事务。
- 估值价格缺失时，`market_value` 和 `equity` 返回 `null`，不能据此生成交易决策。
- 当前没有成交纠错 endpoint。发现错误成交时不要直接修改数据库；必须等待显式补偿事件
  contract 实现。
- daily job 使用稳定 run key 和追加式 attempt lease；并发进程不会重复调用 task，超时
  attempt 先追加失败事件再重试。外部 timer、真实 daily task 与 notification adapter 尚未启用，
  具体边界见[前瞻模拟运维](forward-operations.md)。
