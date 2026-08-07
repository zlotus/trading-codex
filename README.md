# Trading Codex

Trading Codex 是一个个人使用的 AI 辅助 A 股交易决策系统。它将严格受
`as_of` 时间边界约束的历史研究、开盘时段行情快照、可审计的策略配置、
确定性风险控制，以及人工成交后的仓位同步整合到同一个工作区中。

仓库已完成 Milestone 1：本地行情数据基础和 RQAlpha 回测适配可行性验证已经
实现。交易策略、组合账本和 AI 提供方尚未接入，因此页面显示这些组件“未配置”
仍属于正常状态。BaoStock 同步默认完全离线，具体缓存和限流规则见
[本地行情数据指南](data/README.md)。

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

pnpm --dir web preview \
  --host 0.0.0.0 \
  --port 5555 \
  --strictPort
```

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
