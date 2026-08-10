# 项目进度

最后审阅：2026-08-10

## 当前里程碑

Milestone 7 已完成：独立 BaoStock CLI 的 M7.0-M7.5 代码、离线验收和首个真实单项 pilot
均已通过。`sh.600000` 的 2011-2025 前复权日线完成 raw、normalized、逐行 verify 和不可变
completion receipt；完整回填仍属于拟议 Milestone 8。Milestone 4 的真实 OOS 证据仍待补齐；
M5/M6 代码 contract 已实现，但 M4 未关闭前不能启动 M6 前瞻运行，当前 observation 为 0/60。

## 当前基线

仓库现在包含：

- FastAPI 和 React 应用骨架，以及 framework-independent Python 模块边界。
- 内容寻址的不可变 BaoStock raw cache、固定 schema 的 normalized Parquet、来源和
  接收时间 provenance，以及显式 `as_of` 查询。
- instrument、交易日历、历史 universe、日线、复权因子和 5 分钟数据适配；停牌和
  ST 状态保存在规范化日线中，corporate action 使用独立 schema。
- `trading-codex-data` 永久离线，兼容参数 `--fetch-missing` 在 provider import 前退出；只有
  `trading-codex-baostock fetch` 可以加载 BaoStock 网络 adapter。
- M7 CLI 提供 `doctor`、`plan create/show/freeze`、`status`、严格串行 `fetch`、离线
  `sync`/`verify`、`import-raw` 和追加式 `recover`。frozen manifest 固定官方规则 hash、client
  版本、endpoint、完整 query、字段、页数、依赖、空间和预算，并要求无重复键的 canonical JSON。
- 本机 XDG global state 使用跨进程 `flock` 与 append-only SQLite 记录每次 socket attempt，
  同时执行 50,000 provider、45,000 项目、默认双 2,000、`Asia/Shanghai` 自然日、rolling
  24-hour、session/item 和最小 3 秒门禁；`10001011` 只能由管理员确认后的追加事件解除。
- M7 raw 内容寻址且显式 `fsync`；normalized 以每 manifest/dataset 最多 100 万行、2 GiB 的
  immutable segment 原子新增。读取会拒绝物理 schema 漂移和跨 segment 重复键，verify 会
  逐行证明 raw 重放结果存在，冲突和 normalizer 漂移进入 quarantine。
- M7 对同一 data root 的 fetch、status、离线发布、raw import 和恢复使用统一排他锁；请求额度
  拒绝布尔/小数伪整数，每次 socket 预占在事务内二次检查 cooldown，login/logout 的黑名单
  响应保持 `10001011` 硬停止语义。
- `--data-root` 是唯一存储位置参数，可指向任意目录；CLI 不再推断系统盘/数据盘、mount point、
  设备名或 UUID。`doctor --initialize` 自动创建布局，实际写入、`fsync`、atomic replace、`flock`
  和空间门禁保持不变。
- 日线 exact query 接受完整显式日期区间，15 年范围不再被任意切断；实际底层分页仍受 frozen
  `max_pages`、`max_attempts`、session 和全局预算约束。五分钟数据继续按最多 31 个自然日切片。
- raw import 会逐 artifact 验证而不只信任 query index；endpoint normalizer 核对返回代码和日期
  属于 exact query。verify report 按内容 hash 只新增，首次 completion receipt 不可覆盖。
- normalized `PRICE` 保持 `decimal128(20,6)`；BaoStock 更高精度价格在 normalizer 中使用固定
  `ROUND_HALF_EVEN` 显式量化，immutable raw 仍保留 provider 返回的完整小数。
- 数据质量与 09:35 覆盖报告，以及通过 ARM64 验证的 RQAlpha 6.3.0 日频窄适配器。
- ADR-0003 已接受 RQAlpha 作为可替换的回测执行适配器；策略、风险和人工成交仍不
  依赖 RQAlpha。
- 不可变 `DecisionSnapshot` 强制显式 `as_of`，并用 canonical SHA-256 关联输入、
  配置和可重放的决策结果。
- 本地 Parquet 快照源严格配对 BaoStock `adjustflag=2` 前复权信号价与
  `adjustflag=3` 不复权执行价；缺失、future 或不一致数据会 fail closed。
- `DecisionSnapshot` v2 显式携带 `decision_point`、独立状态 universe、日线
  `amount/turnover` 和精确 09:35 五分钟 bar；09:35 决策只读取前一完整交易日的日线，
  风险与下单参考价使用当日 09:35 状态，全部输入受同一 `as_of` 和 provenance 约束。
- 解释型状态管线根据趋势、波动率、宽度、换手、集中度和开盘收益生成 `risk_on`、
  `mean_reverting`、`defensive`、`risk_off` 四种概率及可审计解释。
- 策略池包含波动率缩放动量、短周期反转、防御性低波动和现金；分配器默认只在 09:35
  切换 active strategy，使用 8% 迟滞、20% 单次换手上限和紧急 risk-off 覆盖层，同时
  保留最多 8 个持仓、单票 20% 和 gross exposure 95% 硬边界。
- 前序 `AllocationState` 是 decision hash 的显式输入；`HistoricalReplay` 顺序传递状态，
  ledger 可按明确 `before` 边界恢复最近 M4 base/AI-shadow 分配，不使用隐藏进程状态。
- walk-forward 评估器使用滚动训练窗和不重叠测试窗，统一报告扣费后表现、市场状态切片、
  参数敏感性、最大回撤、alpha/beta、移动 block bootstrap 和 Deflated Sharpe。
- 硬风险和 execution planner 覆盖 stale data、停牌、ST、涨跌停、T+1、整手、
  可卖数量、费用及现金约束；`HistoricalReplay` 直接调用相同 `DecisionPipeline`。
- SQLite 事件账本仅允许追加 decision run、signal、order intent、fill、cash movement、
  signal disposition 和 job attempt；数据库 trigger 拒绝 `UPDATE` 与 `DELETE`。
- ledger schema v4 单独记录 `regime_version` 与 `allocator_version`，并包含 append-only
  `ai_runs`、`ai_messages`、provider health、alert 和 forward observation；v1 迁移只追加
  legacy 标记列及新表，不改写历史 decision payload。
- base、AI-shadow 和 actual 使用同一事件 schema。人工 HTTP 写入只允许 actual track，
  所有人工写操作具备 idempotency key，冲突 payload 会 fail closed。
- 现金与 position lot 按显式 `as_of` 重放；partial fill、费用、T+1、跳过剩余信号和
  缺失估值价格均有确定性处理，信号可追溯至 decision、snapshot 和 source payload。
- EOD preparation 与 09:35 decision 使用稳定 run key 和追加式 attempt lease；并发运行只
  允许一个 task，30 分钟超时 attempt 先追加 `failed` 后再重试，成功 run 不会重复执行。
- Web 已接入决策表、前复权/不复权双价格图、人工成交、跳过信号、三轨权益和持仓
  reconciliation，以及只读 AI 摘要、提案、证据、拒绝原因和对话审计；移动端仅让宽表
  局部横向滚动。
- provider-neutral 异步 LLM 客户端使用严格 JSON schema、版本化 prompt、完整请求
  SHA-256 cache key、调用前 input token 估算、响应后 token/cost 预算、wall-clock timeout
  和不可变文件 cache。
- AI overlay 只接受已批准策略和已发布 `evidence_id`，默认限制单策略权重变化 20%、相对
  base target 增量换手 10%，且不能提高 gross exposure；所有目标重新经过硬风险和
  execution planner，并强制匹配 base pipeline `configuration_id`，任何失败保持 base 不变。
- 离线研究 runner 验证 train/validation/test 独立目录、非重叠日期和完整 artifact hash；
  candidate freeze 前不会向开发阶段暴露 test descriptor。
- BaoStock 前复权日线仍是显式 opt-in，并使用独立 exact-query cache key；主应用、回测、
  scheduler 和旧数据 CLI 均不能回源。
- `/api/v1/system/status` 保持 `research` mode，历史数据、决策内核、账本和回测边界
  已就绪；实时行情与模型 adapter 仍未配置，AI 核心不会因此伪报为可运行。
- `OneShotDailyScheduler` 只处理当前交易日 09:35/15:30 窄窗口，并在 task 前强制 critical
  provider health；health、alert 与 observation 使用 append-only schema，API 只读。
- `trading-codex-ops` 可用 SQLite online backup 创建内容寻址 manifest，校验 hash、
  `quick_check`、foreign keys 与 trigger，并在临时副本 replay 三轨投影；59 日观察会拒绝
  生成归因报告，满 60 日才输出逐日 trace 与四类差异。

## 进行中

M7 当前没有运行中的下载任务。2026-08-10 的首个 pilot 已完成，但独立异卷 backup target、
M8 的历史/预热/train/validation/test 边界和其余 endpoint 单项 pilot 尚未冻结。M4 的真实数据
评估尚未完成。M5 没有 live AI 提案；M6 没有已安装的 timer、真实 daily task、远程通知或
observation。

## 下一步

1. 在启动 M8 bulk wave 前确定另一物理卷的 backup target、复制清单和 hash 复验流程。
2. 审阅并冻结 M8.0 的完整历史、预热、train/validation/test、universe 和 benchmark 边界；
   对其余 endpoint 继续执行一次一个 manifest 的真实 schema pilot。
3. 单项 pilot 全部通过后才分波次补齐双价格、指数成分、09:35 与 corporate action。真实 OOS
   报告通过前 M4、M6 和 live AI 继续保持未启用。

## 风险与限制

- 真实 09:35 样本目前只覆盖 2024-06-03 至 2024-06-07 的 19 个标的，共 95 个
  标的日；样本内覆盖为 95/95，不能外推为全市场或全历史覆盖。
- 当前真实样本的 adjustment factor 和 corporate action 规范化表为空。送股账务仅由
  合成 RQAlpha fixture 验证，真实 provider 映射仍需独立样本。
- 外置 M7 data root 现有 `sh.600000` 前复权日线 3,644 行，但只有一只证券且没有匹配的
  `adjustflag=3` 执行轨，不能构建默认 universe 的双价格决策快照。
- 仓库原有样本仍只有 97 行、19 个标的且全部为 `adjustflag=3`，五分钟数据共 4,656 行；它与
  外置 pilot 尚未形成完整 M4 数据集，更不满足默认 252/63 walk-forward 训练/测试窗。
- 新账本默认从零现金开始，必须先通过 actual cash movement API 追加初始资金；目前
  Web 尚未提供现金变动表单。
- 当前没有成交纠错 endpoint。发现错误 fill 时不能修改数据库，必须等待显式补偿事件
  contract。
- M6 scheduler 目前只是可注入的 one-shot contract，没有真实 calendar/task/provider
  composition，也没有安装外部 timer；合成 health/lease 测试不能视为前瞻运行证据。
- alert 已在账本中记录 `opened/resolved`，但尚无 notification adapter 或真实送达证据。
- forward observation 当前为 0/60。报告 contract 的合成 60 日测试不能替代真实连续观察。
- 当前没有模型 adapter、真实 latency/cost 或 live proposal 证据；右侧 AI 面板只显示已
  存在的 append-only 运行记录，不提供触发运行、审批成交或修改风险配置的入口。
- 研究 runner 防止正常流程在 candidate freeze 前获得 test descriptor；执行不可信研究
  代码时仍需使用独立用户、容器或只读 mount 施加操作系统级权限隔离。
- BaoStock 免费 endpoint 存在封 IP 风险。CLI 无法发现同一 NAT 下的其他客户端；任何 fetch
  都必须人工、串行、无 timer/循环/自动重试，无法协调公网 IP 时停止运行。
- `/mnt/exos_1t/quant/baostock` 已通过真实 write/`fsync`/atomic replace/`flock` 和空间 preflight；
  独立 backup target 仍未确定，因此不得启动 M8 bulk wave。
- `query_history_k_data_plus` 日线的 `adjustflag=2` 已完成真实 pilot；批量单日日线、复权因子、
  指数成分、5 分钟和 dividend endpoint 仍只有 fake fixture。Dividend 缺少日内公告时间，
  当前 `available_at` 保守使用公告日后一个自然日 00:00。
- M7 v1 的 sync/verify 为严格查重会扫描当前 dataset 并构造内存表；单项 pilot 和有界波次已有
  保护，但 M8 全历史执行前仍须用真实行数测量峰值内存，必要时先引入 DuckDB/磁盘索引门禁。
- RQAlpha 当前固定为 6.3.0 并隔离运行。用途变为商业场景或升级版本前，需要重新
  核对源码许可说明和全部 adapter fixture。

## 验证

- 2026-08-10：真实 `doctor --initialize` 在 `/mnt/exos_1t/quant/baostock` 通过；目标盘约
  1 TB、剩余约 183 GB，global state `integrity=ok` 且 16 个 append-only trigger 完整。
- 2026-08-10：manifest `bs-af5dfdaa19fc5c6ae075` 以 30 秒间隔完成 `login/query/page/logout`
  共 4 次成功 attempt，无 incident；raw hash `2b48d48e...808a9` 保存 3,644 行。
- 2026-08-10：真实 sync 发布 196,191-byte 日线 segment；逐行 verify 得到 0 duplicate、0 missing、
  0 mismatch、0 quarantine，并写入内容 hash 报告 `052d9a1c...f3b78` 和不可变 completion receipt。
- 2026-08-10：前复权 10 位价格兼容修复后 `.venv/bin/pytest` 通过，136 个测试；包含 M1-M6
  原有覆盖，以及 M7 manifest、真实精度回归、
  跨进程/global 与 data-root 锁、自然日/rolling 24-hour/冷却预算、login/query/page/logout、
  `10001011`、raw 完整性、exact-query、Parquet segment、quarantine、immutable completion
  receipt 和 offline-only CLI 回归测试。
- 2026-08-10：`.venv/bin/ruff check .`、`UV_CACHE_DIR=/tmp/trading-codex-uv-cache
  uv lock --check`、Markdown 本地链接检查与 `git diff --check` 通过。
- 2026-08-09：`pnpm --dir web build` 通过，Vite 6.4.3。
- 2026-08-09：`PYTHONPATH=backend/src .venv/bin/python -m
  trading_codex.ai.research_cli --help` 通过，隔离研究 CLI 入口可用。
- 2026-08-09：`PYTHONPATH=backend/src .venv/bin/python -m
  trading_codex.operations.cli --help` 通过，备份、校验、replay 和前瞻报告入口可用。
- 2026-08-09：`UV_CACHE_DIR=/tmp/trading-codex-uv-cache uv run --frozen
  trading-codex-baostock --help` 通过，已安装 entrypoint 只暴露一个 `--data-root` 存储位置参数，
  M7 子命令和唯一 live `fetch` 边界可见。
- 2026-08-09：`ImmutableRawStore.iter_verified(source="baostock")` 只读校验现有
  `data/raw` 的 70 个 raw artifact 全部通过，可进入后续离线 import pilot。
- 2026-08-09：Chromium 132.0.6834.159 使用合成 AI-shadow ledger 检查 1440×1000 和
  390×844 CSS viewport；摘要、提案、对话 panel 无重叠，移动端 document width 为
  390px，720px 信号表仅在局部 `.table-scroll` 内横向滚动。
- 2026-08-09：`git diff --check` 通过。
- 2026-08-08：只读 `trading-codex-data quality` 通过；当前 instruments 8,885 行、
  historical universe 39,557 行、日线 97 行、五分钟 4,656 行，缺少前复权日线、
  adjustment factor 和 corporate action 数据。
- 2026-08-08：Chromium 对带部分成交数据的今日决策和组合对账完成 1440×1000 与真实
  390×844 CSS viewport 检查；全页无横向溢出，决策宽表保留局部滚动。
- 2026-08-08：RQAlpha spike 在 `aarch64`、Python 3.12.3、RQAlpha 6.3.0 下
  通过 T+1、手数、停牌、涨跌停、费用和送股 fixture。
- 2026-08-08：19 标的离线同步重放得到 `cache_hits=19`、`cache_misses=0`、
  `upstream_requests=0`，数据质量报告状态为 `passed`。
- 2026-08-08：2024-06-03 至 2024-06-07 的 09:35 覆盖评估为 95/95，calendar
  和 historical universe 前置数据均完整。
