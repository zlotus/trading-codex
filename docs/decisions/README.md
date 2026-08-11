# 架构决策记录

ADR 用于保存长期有效的项目决策及其依据。已接受的记录属于历史证据：决策发生
变化时，应新增 ADR 并将旧记录标记为已被取代，而不是删除或改写旧记录。

| ADR | 状态 | 决策 |
| --- | --- | --- |
| [0001](0001-shared-decision-core.md) | Accepted | 使用共享决策核心和可替换的执行适配器 |
| [0002](0002-bounded-ai-authority.md) | Accepted | 约束 AI 权限并保留三条组合轨道的归因 |
| [0003](0003-adopt-rqalpha-backtest-adapter.md) | Accepted | 采用 RQAlpha 作为受限且可替换的回测执行适配器 |
| [0004](0004-immutable-dual-price-decision-snapshot.md) | Accepted | 使用不可变双价格快照驱动共享决策管线 |
| [0005](0005-append-only-sqlite-ledger.md) | Accepted | 使用 append-only SQLite 事件账本和派生组合视图 |
| [0006](0006-explicit-regime-and-stateful-allocation.md) | Accepted | 使用显式市场状态输入和可重放的受约束分配 |
| [0007](0007-external-one-shot-forward-operations.md) | Accepted | 使用外部一次性调度和 append-only 前瞻运维证据 |
| [0008](0008-manifest-driven-serial-baostock-downloader.md) | Superseded by ADR-0009 | 使用清单驱动且全局串行的独立 BaoStock 下载边界 |
| [0009](0009-data-root-directed-baostock-storage.md) | Superseded by ADR-0010 | 由 data root 直接决定 BaoStock 落盘位置 |
| [0010](0010-unix-style-raw-baostock-pipeline.md) | Accepted | 使用 raw-only、存在即跳过的 Unix 风格 BaoStock 工具链 |
