# RQAlpha 账务适配 Spike

本目录只用于 Milestone 1 的隔离可行性验证，不属于主应用运行时依赖。Spike
使用合成的 20 只证券、5 个交易日和一个送股事件，不下载 RQAlpha bundle，也不
访问 BaoStock。

在单独的临时环境中安装项目和锁定的 RQAlpha 版本后运行：

```bash
uv venv /tmp/trading-codex-rqalpha --python 3.12
uv pip install --python /tmp/trading-codex-rqalpha/bin/python -e .
uv pip install --python /tmp/trading-codex-rqalpha/bin/python \
  -r spikes/rqalpha/requirements.txt
/tmp/trading-codex-rqalpha/bin/python spikes/rqalpha/run_spike.py
```

脚本对 T+1、普通 A 股下单手数、停牌、涨跌停、最低佣金、印花税和送股后的
数量/成本进行独立断言。任何断言不一致都会返回非零退出码。

## 已验证结果

2026-08-08 在 `aarch64`、Python 3.12.3、RQAlpha 6.3.0 环境中运行通过：20 个
instrument、5 个交易日和全部账务断言均匹配 fixture。运行时使用 RQAlpha 6.3.0
的 `context.now`，不依赖旧版本已移除的 `rqalpha.api.get_datetime`。

该结论只支持把 RQAlpha 作为日频回测的窄适配器。它不证明分钟级适配、真实
corporate action 映射或更长历史区间已经完成；这些边界记录在 ADR-0003。
