# Trading Codex

Trading Codex 是一个个人使用的 A 股行情分析与低频策略工作区。它将严格受
`as_of` 时间边界约束的历史研究、低频策略生成/回测/调试、可审计的交易提案、
确定性风险控制，以及人工成交和交易记录整合到同一个工作区中。AI 只提供受约束的研究与
分配提案；系统不连接券商自动执行，也不面向高频或亚分钟交易。

仓库当前并行推进 Milestone 4 的真实数据验收，以及 Milestone 5/6 的受限 AI 与前瞻运维
contract。共享决策内核已包含可解释市场状态、四策略池、受约束分配和 walk-forward 评估；
运维层已包含 provider health gate、append-only 告警、并发 attempt lease、一致性备份、
replay 和 60 日归因门槛。M8.2 已补齐 2011-01-01 至 2026-08-10 的逐交易日沪深300/中证500
universe、中证800 benchmark 和成员双价格覆盖；正式 OOS 仍缺真实 corporate action、经批准
范围内的 09:35 覆盖及冻结后的 M8.4 artifact。
daily task、实时行情与模型 adapter 也尚未接入，因此系统不会启动前瞻调度或 live AI inference，
真实观察仍为 0/60 个交易日。主应用和数据处理默认完全离线，只有独立 raw 下载器可访问 BaoStock；
具体幂等和限流规则见
[本地行情数据指南](data/README.md)，账本边界见[组合账本操作指南](docs/ledger-operations.md)，
M6 状态见[前瞻模拟运维](docs/forward-operations.md)。

## 仓库结构

```text
backend/            FastAPI 应用和 Python 领域模块
web/                React 决策工作区前端
data/               本地行情数据目录；实际数据不会提交到 Git
docs/               项目上下文、实施计划、进度和 ADR
artifacts/          回测及实验生成物
```

## 环境要求

- Python 3.12
- `uv`
- Node.js
- `pnpm`

首次拉取代码后安装锁定版本的依赖：

```bash
cd /home/radxa/quant/trading-codex

uv sync --frozen
pnpm --dir web install --frozen-lockfile
```

## 本地开发

分别在两个终端中启动后端和前端：

```bash
make dev-api
make dev-web
```

- 前端：<http://127.0.0.1:5173>
- API 文档：<http://127.0.0.1:8000/docs>

开发模式提供热更新，适合修改代码时使用。

若本机 `8000` 已被其他服务占用，可以把后端启动到备用端口，并为 Vite 指定开发代理：

```bash
# 终端 1
uv run uvicorn trading_codex.main:app \
  --app-dir backend/src \
  --reload \
  --host 127.0.0.1 \
  --port 8012

# 终端 2
API_ORIGIN=http://127.0.0.1:8012 pnpm --dir web dev --host 127.0.0.1 --port 5173
```

`API_ORIGIN` 只接受不含 credentials、path、query 或 fragment 的 HTTP(S) origin；未设置
时仍使用 `http://127.0.0.1:8000`。

## 本地构建并发布到 5555 端口

当前应用由两个进程组成：浏览器访问 `5555` 端口上的前端，前端再将
`/api/*` 请求代理到仅监听本机的 FastAPI `8000` 端口。

```text
浏览器 :5555 -> Vite 前端
                    └─ /api/* -> FastAPI :8000
```

### 1. 构建前端

在仓库根目录执行：

```bash
cd /home/radxa/quant/trading-codex
pnpm --dir web build
```

构建产物位于 `web/dist/`。

### 2. 启动后端

打开第一个终端：

```bash
cd /home/radxa/quant/trading-codex

TRADING_CODEX_ENVIRONMENT=production \
uv run uvicorn trading_codex.main:app \
  --app-dir backend/src \
  --host 127.0.0.1 \
  --port 8000
```

后端只监听回环地址，不直接暴露给局域网。API 文档和健康检查地址为：

- <http://127.0.0.1:8000/docs>
- <http://127.0.0.1:8000/api/v1/health>

### 3. 在 5555 端口启动前端

打开第二个终端：

```bash
cd /home/radxa/quant/trading-codex

PUBLIC_ORIGIN=https://trade.example.com \
pnpm --dir web preview \
  --host 0.0.0.0 \
  --port 5555 \
  --strictPort
```

将 `trade.example.com` 替换为实际使用的公网域名；变量只接受完整的 HTTP(S)
origin。修改后必须重启 `vite preview`，Vite 才会重新加载精确 Host 白名单。

`--strictPort` 会在 `5555` 已被占用时直接报错，避免 Vite 自动改用其他
端口。当前 Vite 配置会让预览服务沿用 `/api` 代理，因此不需要把后端的
`8000` 端口暴露到局域网。

### 4. 访问应用

本机访问：

```text
http://127.0.0.1:5555
```

同一局域网内的其他设备访问时，先查询本机地址：

```bash
hostname -I
```

选择类似 `192.168.x.x` 或局域网实际使用的地址，不要选择 Clash/TUN
创建的虚拟地址，然后访问：

```text
http://<设备局域网 IP>:5555
```

如果其他设备无法连接，检查主机防火墙是否允许局域网访问 TCP `5555`
端口。

### 5. 验证与停止

可以分别验证后端直连和前端代理：

```bash
curl http://127.0.0.1:8000/api/v1/health
curl http://127.0.0.1:5555/api/v1/health
```

两个请求都应返回状态为 `ok` 的 JSON。两个终端必须保持运行；在各终端
按 `Ctrl+C` 即可停止服务。

`vite preview` 适合本机或可信局域网内的个人使用和构建验证，不应直接
作为面向公网的生产服务器。需要开机自启或后台常驻时，应另行配置
`systemd` 或等价的进程管理方案。

需要复用本机现有 Cloudflare Tunnel 并通过 Zero Trust Access 从互联网访问
时，请阅读 [Cloudflare Zero Trust 部署指南](docs/cloudflare-zero-trust-deployment.md)。

## 质量检查

```bash
make test
make lint
make build-web
```

开始功能开发前，请先阅读[项目文档索引](docs/README.md)和
[实施计划](docs/implementation-plan.md)。
