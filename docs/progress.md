# 项目进度

最后审阅：2026-08-13

## 当前里程碑

Milestone 7 的 raw-only BaoStock 工具链、M8.0 固定成分基础回填、M8.1 固定成分 EOD 工程
smoke 和 M8.2 point-in-time universe/benchmark 数据边界均已完成。M8.2 覆盖 2011-01-01 至
2026-08-10 的 3,789 个交易日，逐日使用沪深300/中证500有效成分并以中证800 `sh.000906`
作为 benchmark。

M8.2 只关闭数据覆盖切片，不关闭 Milestone 4。真实 corporate action、经批准范围内的 09:35
数据和冻结后的 untouched OOS artifact 仍属于 M8.3/M8.4；所有 M8.2 报告固定声明
`formal_m4_oos=false`。M5/M6 contract 已实现，但 M4 未关闭前不能启动前瞻运行，当前真实
observation 仍为 0/60。

M8.2 源码、测试和文档已完成；提交和远端同步状态以 `main`/`origin/main` 的 Git refs 为准。

## 当前基线

- 产品是本地单用户的 A 股行情分析、低频策略生成/回测/调试和人工交易记录工作区；自动券商
  执行、亚分钟策略和高频交易均为范围外。
- 共享 `DecisionPipeline`、显式市场状态、四策略池、受约束分配、硬风险、execution planner、
  walk-forward、RQAlpha 6.3.0 窄适配器和 append-only 三轨账本已实现。
- Web 已包含决策、双价格图、人工成交、跳过信号、三轨持仓/权益对账，以及只读 AI 审计视图。
- AI 只能生成受约束提案；任何目标仍须经过确定性风险和执行检查，不能创建真实成交。
- `trading-codex-baostock` 是唯一 live 数据入口：全局 `flock`、单会话严格顺序、每次 socket send
  计数、默认 40,000 次停止、首错停止且无自动重试。目标 raw 文件存在时零网络跳过。
- socket 响应设置 60 秒超时；EOF、不完整响应、解码或传输错误立即 fail closed，不发布半截 raw。
- `inspect-raw` 与 `ingest-raw` 分别重新验证 envelope。ingest 可去除跨 payload 的完全相同业务
  行，但任何业务值冲突都会拒绝该 payload；所有处理均离线。
- `ParquetDataStore.scan` 和 `daily_bar_series` 使用显式 `as_of`、Arrow 谓词下推和列裁剪，避免
  在每个决策日完整读取外置盘数据。
- `PointInTimeEodView` 逐日选择指数成分，严格配对 `adjustflag=2` 信号价和 `adjustflag=3`
  执行价，允许退出指数但仍持有的股票只为估值、风险和退出继续读取。
- 原始 `index_memberships` 完整保留；策略有效成员按 instrument `[ipo_date, out_date)` 半开区间
  过滤。任一有效成员缺 universe、双价格、交易状态或 benchmark 时覆盖门禁失败。
- `/api/v1/system/status` 仍为 `research` mode；实时行情、模型 adapter、真实 daily task、timer、
  远程通知和 live AI proposal 均未配置。

## M8.2 真实数据

- 第一阶段生成 11,368 条逐交易日指数成分、historical universe 和中证800请求；第二阶段生成
  3,670 条曾任成员双价格请求。
- 第二阶段严格顺序运行完成：下载 2,070，已有文件跳过 1,600，剩余 0，共 3,878 次 socket send；
  墙钟 2:53:19，峰值 RSS 137,628 KiB。
- 最终 raw 为 15,045/15,045 个有效 envelope，`warnings=[]`，离线 inspect 墙钟 8:12.77。
- 最终 ingest 新发布 2,070 个日线 segment、6,751,082 行；去除 7,288 个相同业务键，业务冲突
  为 0，`warnings=[]`。墙钟 28:12.07，峰值 RSS 4,379,052 KiB。
- 历史指数原始快照包含 3,031,178 个成员日和 1,835 个唯一代码。41 个成员日落在 instrument
  上市区间外，原始快照保留，但不进入策略候选。
- 过滤后的 3,031,137 个有效成员日，其 historical universe、前复权和不复权覆盖均为
  3,031,137/3,031,137；中证800为 3,789/3,789，所有 issue count 为 0。
- BaoStock 在 2019-01-07 至 2019-01-18 的 10 个交易日、2021-09-13 至 2021-09-30 的 12 个
  交易日只返回 499 个中证500成员。这 22 个已核验 vacancy 原样保留，不补造第 500 个成员。
- 历史成分是 2026-08-12 获取的 provider 重建视图，`source_received_at` 保留真实接收时间；
  因此仍存在 provider revision bias，不能声称是当日 contemporaneous archive。

## 下一步

1. M8.3 独立对账至少一个真实现金分红和一个送转样本，并只为明确采用 09:35 contract 的研究
   范围补分钟线。
2. M8.4 在查看正式结果前冻结数据描述、代码、成本、参数和测试区间，生成可重放的 untouched
   OOS artifact；人工审阅后再判断 M4 是否关闭。
3. 只有 M4 正式关闭且 live provider/task 配置完成后，才允许启动 M6 的 60 个真实交易日前瞻
   observation。

## 风险与限制

- M8.2 覆盖报告不是收益报告，也不是正式 OOS 证据；不得把 `formal_m4_oos=false` 改写为 true。
- adjustment factor 和 corporate action 真实规范化样本仍未完成独立对账；送股账务目前只有合成
  RQAlpha fixture 证据。
- 真实 09:35 样本仍只有 2024-06-03 至 2024-06-07 的 19 个标的、95 个标的日，不能外推到
  全市场或全历史。
- BaoStock 免费 endpoint 有封 IP 风险。程序无法统计同一 NAT 下其他机器的流量，下载期间仍须
  保证同一公网 IP 没有第二个客户端。
- raw 完成后的备份仍是独立运维责任；当前外置盘约 932 GiB，已使用约 772 GiB，可用约 160 GiB。
- M8.2 耗时和内存来自 RK3588、7200 RPM 机械盘及 NTFS-3G 数据布局，只是本机工程证据，不是
  跨机器 SLA。
- actual 组合仍缺现金变动 Web 表单和成交纠错 endpoint；纠错必须继续使用未来定义的补偿事件，
  不能修改历史账本。

## 验证

- 2026-08-12：最终 `assess-point-in-time` 读取 15,041 个 provenance payload，覆盖报告
  `point-in-time-coverage-7737f3e2dae5ecd4.json` 为 `status=passed`、`formal_m4_oos=false`，全部
  12 类 issue count 为 0；运行墙钟 1:55.77、峰值 RSS 5,401,872 KiB。
- 2026-08-12：真实 `PointInTimeEodView` 窄范围 smoke 成功构建 2026-08-10 EOD 快照：800 个
  候选、1,600 条双价格 bar，中证800日收益 `0.00291175`，覆盖 issue 全 0；冷加载墙钟
  2:00.33、峰值 RSS 463,380 KiB，无网络访问。
- 2026-08-12：`.venv/bin/pytest -q` 通过 172 个测试；`.venv/bin/ruff check .`、
  `UV_CACHE_DIR=/tmp/trading-codex-uv-cache uv lock --check`、`pnpm --dir web build`、38 个 Markdown
  本地链接和 `git diff --check` 均通过。
- 2026-08-11：M8.1 固定 800 标的、528 个交易日、默认 252/63 和两组换手参数完整运行；报告
  明确 `survivorship_bias=true`、`formal_m4_oos=false`，不能作为正式策略表现结论。
