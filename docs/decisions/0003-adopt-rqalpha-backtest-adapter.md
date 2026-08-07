# ADR-0003: 采用 RQAlpha 作为受限的回测执行适配器

- Status: Accepted
- Date: 2026-08-08
- Supersedes: None
- Superseded by: None

## 背景

ADR-0001 要求策略、配置、分配和风险逻辑保持 framework-independent，同时用一个
可替换的执行适配器承担 A 股撮合和账务。Milestone 1 因此需要先验证 RQAlpha 在
目标 ARM64 主机上的安装兼容性，并用独立 fixture 检查关键交易规则，不能只因框架
宣称支持 A 股就直接采用。

## 决策

采用 RQAlpha 6.3.0 作为首个回测执行适配器，边界如下：

1. RQAlpha 只负责历史回放中的撮合、交易约束、费用和持仓账务，不进入策略、分配、
   风险或人工成交领域模型。
2. 适配器只从本项目的 normalized Parquet 读取数据，并强制要求显式 `as_of`；不下载
   或依赖 RQAlpha bundle。
3. 当前接受范围是日频股票回测。分钟级数据、tick、期货和实时 snapshot 不在本决策
   的已验证范围内。
4. RQAlpha 保留在独立、锁定的 Python 环境中，不加入主应用运行时依赖。版本固定在
   `spikes/rqalpha/requirements.txt`。
5. 若后续版本漂移、许可、ARM64 兼容性或未覆盖规则破坏该边界，则在同一 adapter
   contract 后替换为窄自定义 simulator，不改写策略核心。

## 验证依据

2026-08-08 在 `aarch64`、Python 3.12.3 和 RQAlpha 6.3.0 的隔离环境中，使用 20 个
合成证券、5 个交易日和 1 个送股事件完成 spike。结果与独立 fixture 一致：

- 150 股订单按普通 A 股手数约束为 100 股；同日卖出被拒，下一交易日 100 股可卖。
- 停牌买入、涨停买入和跌停卖出均被拒。
- 100 股买入再卖出的最低佣金及印花税合计为 11 元。
- 10 送 10 后持仓由 100 股变为 200 股，平均成本由 10 元变为 5 元。
- 20 个 instrument 均由 normalized Parquet adapter 加载，未访问 BaoStock 或下载
  bundle。

这是一项合成 contract spike，不代表真实 corporate action 的 provider 映射已经被
验证。真实 BaoStock 样本仅用于数据质量和 09:35 覆盖评估。

## 理由

实测表明 RQAlpha 能在目标架构上承担最容易出错的 A 股账务规则，同时窄适配边界
避免框架 API 扩散到共享决策核心。与立即编写自定义撮合器相比，这降低了 T+1、费用
和 corporate action 会计的初始正确性风险，并保留后续替换路径。

## 考虑过的方案

- 立即实现窄自定义 simulator：暂不采用，因为当前 RQAlpha spike 已通过，重复实现
  持仓和 corporate action 账务没有足够收益。
- 让策略直接使用 RQAlpha API：拒绝，因为这会违反 ADR-0001，并使历史与日常决策
  产生两套语义。
- 把 RQAlpha 加入主应用依赖：拒绝，因为主 API/worker 不需要它，隔离环境能缩小
  依赖、版本和许可影响面。

## 后果

- 后续必须为 adapter contract 保留独立 fixture；升级 RQAlpha 前重新运行全部规则。
- corporate action 的真实数据映射、分钟级支持和更长区间仍需单独验证，不能由本
  spike 推断为已完成。
- 当前安装包 metadata 标注 `Apache-2.0`，但已安装源码头部对商业使用另有授权说明。
  本项目当前是个人、非商业用途；用途变化前必须重新核对许可并取得必要授权。
- RQAlpha 生成的 fill 仍是模拟结果，不能绕过确定性风险检查，也不能写入人工成交
  账本。
