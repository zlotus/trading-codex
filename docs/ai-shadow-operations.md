# AI 研究与影子分配

Milestone 5 把模型输出限制在 provider-neutral 的结构化提案边界内。AI 不能创建成交、
修改风险配置、启用未知策略，或绕过共享的 `HardRiskEngine` 和 `ExecutionPlanner`。

```text
base decision + versioned evidence
  -> structured LLM client
  -> boundary validation
  -> deterministic risk and execution planning
  -> ai_shadow decision + append-only audit
```

## 客户端 contract

`ProviderNeutralLLMClient` 只依赖可注入的异步 `LLMTransport`。transport 接收 provider、
model、prompt version、JSON schema、完整 messages、输出 token 上限和 timeout，不接触
ledger、fill API 或风险配置。

每个请求执行以下 fail-closed 检查：

- cache key 包含 provider、model、完整 prompt、response schema 和预算配置。
- 调用前按完整请求估算 input tokens；响应后再次核对 provider 返回的 input、output、
  total tokens 和估算成本。
- `asyncio.wait_for` 强制 wall-clock timeout；迟到响应不会进入分配管线。
- 响应必须是精确 JSON object，不允许额外字段；Decimal 使用字符串编码。
- `FileCompletionCache` 使用 SHA-256 分目录、原子创建和不可变冲突检查。cache 中不保存
  credential；provider adapter 也不得把 secret 放入 request payload。

仓库目前没有绑定任何具体模型 adapter，也没有暴露触发 AI 运行的 HTTP endpoint。
系统状态因此保持 `ai=not_configured`。接入 provider 时，应在内部 daily job composition
root 注入 transport，并显式配置预算与 timeout；不能把调用能力传给 Web 客户端。

## 提案边界与 fallback

结构化提案只能包含：

- 与 base decision 和 snapshot 精确匹配的 ID、`generated_at` 和 `valid_until`。
- 已批准策略的权重，且总和必须为 1。
- `risk_scale`，范围为 `[0, 1]`。
- 只引用本次 versioned context 中已发布 `evidence_id` 的证据。
- 摘要、理由和一条 assistant 审阅消息。

默认 overlay 额外限制单个策略相对基础权重最多变化 20%，AI-shadow 相对 base target 的
增量换手最多为 10%，gross exposure 不能高于 base target。通过边界后，目标仍重新运行
确定性风险和 execution planning。`BoundedAIOverlay` 必须从生成 base decision 的同一个
`DecisionPipeline` 构造，并校验 `configuration_id`；不能静默回落到另一组默认风险参数。
未知策略、未知证据、时点错误、越界、timeout、预算超限、provider 异常或无效 JSON
都会保留 base target；结构化有效但越界的记录标记为 `rejected`，没有可用结构化提案时
标记为 `fallback`。

## 审计与查询

ledger schema v3 增加 `ai_runs` 和 `ai_messages`。两张表与 decision、snapshot、prompt、
provider/model、usage、cache、结构化提案、验证错误和 AI-shadow decision 建立不可变关联；
SQLite trigger 拒绝 `UPDATE` 与 `DELETE`。

只读查询：

```bash
curl http://127.0.0.1:8000/api/v1/ai/workspace
curl 'http://127.0.0.1:8000/api/v1/ai/workspace?as_of=2026-08-09T02:00:00Z'
```

Web 右侧面板显示最新摘要、证据、策略权重、相对 base target 的变化、拒绝原因、usage、
cache 状态和 assistant 消息。当前没有对话写入和历史列表 endpoint。

## 隔离研究数据

离线研究 manifest 必须声明互不嵌套的 train、validation、test 本地目录、严格先后且不
重叠的日期区间，以及每个目录的 SHA-256。验证命令会重新读取全部文件并拒绝 symlink、
hash 漂移、目录重叠和相同 artifact：

```json
{
  "version": "isolated-ai-research-v1",
  "splits": {
    "train": {
      "root": "./train",
      "start_date": "2018-01-01",
      "end_date": "2022-12-31",
      "content_sha256": "<sha256>"
    },
    "validation": {
      "root": "./validation",
      "start_date": "2023-01-01",
      "end_date": "2023-12-31",
      "content_sha256": "<sha256>"
    },
    "test": {
      "root": "./test",
      "start_date": "2024-01-01",
      "end_date": "2024-12-31",
      "content_sha256": "<sha256>"
    }
  }
}
```

```bash
trading-codex-ai-research validate path/to/manifest.json
```

`OfflineResearchRunner.freeze()` 在开发阶段只传递 train 和 validation；candidate payload
完成 canonical freeze 并生成 hash 后，`evaluate()` 才向评估器传递 test descriptor。
目录隔离和 runner contract 防止正常研究流程意外看到 test；若要执行不可信研究代码，
仍必须在操作系统层使用独立用户、容器或只读 mount 强制权限隔离。
