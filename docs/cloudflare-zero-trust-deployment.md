# 通过 Cloudflare Zero Trust 发布 Trading Codex

本文说明如何复用本机已经运行的 Cloudflare Tunnel，将 Trading Codex 通过
Cloudflare Access 身份验证后发布到互联网。示例公网地址使用
`trade.example.com`，实际操作时替换为你自己的域名。

## 安全边界

- 必须先创建 Cloudflare Access 应用和仅允许本人登录的策略，再添加
  Published application route。否则保存 route 后，站点会短暂对所有互联网
  用户开放。
- 公网只发布前端入口 `127.0.0.1:5555`。FastAPI `127.0.0.1:8000` 继续只在
  本机监听，通过 Vite 的 `/api` 代理访问。
- 不需要在路由器或主机防火墙中开放 `5555`、`8000` 入站端口；
  `cloudflared` 主动向 Cloudflare 建立出站连接。
- 不要把 tunnel token、`/etc/cloudflared/token`、Access 凭据或真实仓位数据
  写入仓库。
- Cloudflare Access 是外围身份验证，不替代应用自身的风险控制、审计和数据
  完整性校验。

```text
互联网浏览器
    |
    v
Cloudflare Access 身份验证
    |
    v
Cloudflare Edge -> 现有 Tunnel -> 本机 cloudflared
                                      |
                                      v
                              127.0.0.1:5555
                                Vite preview
                                      |
                         /api/* ------+------> 127.0.0.1:8000
                                                   FastAPI
```

## 本机已确认的基础

截至 2026-08-07，本机环境为：

- `cloudflared 2026.7.3`，实际二进制为 `/usr/bin/cloudflared`。
- `cloudflared.service` 已启用并运行。
- systemd 通过 `tunnel run --token-file /etc/cloudflared/token` 启动，说明这是
  Zero Trust 控制台远程管理的 tunnel。
- 本地管理端点为 `http://127.0.0.1:20241`。

因此本机不需要再次执行 `cloudflared tunnel login`、`cloudflared tunnel
create` 或 `cloudflared service install`，也不应为了本应用删除或替换现有
tunnel。公网 hostname 和 origin 参数应在 Cloudflare 控制台配置。

运行状态会随时间变化，部署前重新检查：

```bash
systemctl is-active cloudflared
systemctl is-enabled cloudflared
curl -fsS http://127.0.0.1:20241/ready
```

前两个命令应分别输出 `active` 和 `enabled`，`/ready` 应返回 HTTP 200，并且
`readyConnections` 大于 `0`。

## 前置条件

开始前准备以下信息：

- 一个已经接入同一 Cloudflare 账号的有效域名。
- 一个专用于本应用的一级子域名，例如 `trade.example.com`。尽量避免
  `trade.home.example.com` 这类多级子域名，以免需要 Advanced Certificate。
- 用于登录的本人邮箱，或已经接入 Zero Trust 的身份提供方账号。
- Cloudflare 账号中能够编辑 Zero Trust Access、Tunnel route 和 DNS 的权限。

先在本机安装依赖并构建：

```bash
cd /home/radxa/quant/trading-codex

uv sync --frozen
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
```

## 1. 在本机验证应用

先用两个终端验证应用自身，避免把本地进程故障误判为 tunnel 故障。

终端一启动 API：

```bash
cd /home/radxa/quant/trading-codex

TRADING_CODEX_ENVIRONMENT=production \
uv run uvicorn trading_codex.main:app \
  --app-dir backend/src \
  --host 127.0.0.1 \
  --port 8000
```

终端二启动构建后的前端：

```bash
cd /home/radxa/quant/trading-codex

pnpm --dir web preview \
  --host 127.0.0.1 \
  --port 5555 \
  --strictPort
```

这里故意只监听 `127.0.0.1`。Cloudflare Tunnel 和应用位于同一台机器，不
需要用 `0.0.0.0` 暴露给整个局域网。

验证后端和前端代理：

```bash
curl -fsS http://127.0.0.1:8000/api/v1/health
curl -fsS http://127.0.0.1:5555/api/v1/health
```

两个请求都应返回状态为 `ok` 的 JSON，之后再配置公网入口。

## 2. 配置登录方式

如果 Zero Trust 已接入你常用的身份提供方，可以继续使用。个人部署也可以
使用邮件 One-time PIN：

1. 打开 Cloudflare dashboard。
2. 进入 **Zero Trust > Integrations > Identity providers**。
3. 在 **Your identity providers** 下选择 **Add new identity provider**。
4. 选择 **One-time PIN** 并保存。

One-time PIN 只会向 Access policy 允许的邮箱发送验证码。它依赖邮箱账号的
安全性；如果已有支持 MFA 的身份提供方，优先使用后者。

## 3. 先创建 Access 应用

务必在创建 tunnel route 之前完成本节：

1. 进入 **Zero Trust > Access controls > Applications**。
2. 选择 **Create new application**。
3. 选择 **Self-hosted and private**。
4. 选择 **Add public hostname**，填入 `trade.example.com`，路径留空，以保护
   整个站点及 `/api/*`。
5. 添加 Access policy：
   - Action 选择 **Allow**。
   - Include 选择 **Emails**，只填写本人的完整邮箱地址。
   - 不要使用 **Everyone**、**Bypass** 或宽泛的邮箱域名规则。
6. 选择需要的身份提供方。只有一个身份提供方时，可以启用
   **Apply instant authentication**。
7. Session Duration 建议不超过 24 小时；身份提供方支持时启用 MFA。
8. 保存应用。

Access 应用默认拒绝未匹配 Allow policy 的用户。保存后再次核对 hostname，
避免 Access 保护的是一个域名，而 tunnel 发布的是另一个域名。

## 4. 给现有 Tunnel 添加 Published Application

Cloudflare 控制台不同版本可能显示以下两种入口之一：

- **Networking > Tunnels**；或
- **Zero Trust > Networks > Connectors > Cloudflare Tunnels**。

选择本机正在使用且状态为 `Healthy` 的现有 tunnel，然后：

1. 打开 **Routes** 或 **Published application routes**。
2. 选择 **Add route > Published application**。
3. Hostname 填写与 Access 应用完全相同的 `trade.example.com`，路径留空。
4. Service URL 填写 `http://127.0.0.1:5555`。
5. 在 **Additional application settings** 中设置：
   - **HTTP Host Header**：`localhost`。
   - **Protect with Access**：启用。
6. 保存 route。

`HTTP Host Header=localhost` 是当前 Vite 预览服务所需的兼容设置。否则
Cloudflare 可能把公网域名作为 `Host` 头传给 Vite，Vite 会以
`Blocked request. This host is not allowed` 返回 403。不要用
`allowedHosts: true` 关闭 Vite 的 Host 校验。

启用 **Protect with Access** 后，`cloudflared` 会在把请求转发给本地应用前
验证 Access JWT。这能降低 route 或请求链配置错误时绕过 Access 的风险。
如果控制台要求填写 Team name 或 AUD tag，应从刚创建的 Access 应用概览中
读取对应值，不要根据 hostname 猜测。

如果域名由 Cloudflare 完整托管，保存 route 时会自动创建 DNS 记录。如果
使用 partial CNAME setup，则需要按控制台提示在权威 DNS 服务商处创建 CNAME。

## 5. 从互联网验证

先在未登录的无痕窗口，或使用移动网络的设备访问：

```text
https://trade.example.com
```

正确结果应先出现 Cloudflare Access 登录流程。也可以在任意外部终端检查：

```bash
curl -sS -D - -o /dev/null https://trade.example.com
```

未认证请求通常会被重定向到 Access 登录页或被拒绝，不应直接返回应用首页。
如果无痕窗口无需登录就能看到应用，立即删除或禁用 Published application
route，修正 Access hostname 和 policy 后再发布。

完成登录后检查：

- 页面正常加载。
- 浏览器开发者工具中 `/api/v1/system/status` 返回 200。
- 刷新页面不会绕过或重复触发异常登录。
- 未在 Allow policy 中的另一个邮箱无法登录。

建议再为该 hostname 建立 Cache Rule，选择 **Bypass cache**，避免未来包含
个人仓位或决策结果的响应被边缘缓存。不要为该 hostname 启用 APO、公开页面
缓存或绕过 Access 的规则。

## 6. 配置应用开机自启

Cloudflare Tunnel 已经由 systemd 常驻，但前后端也必须常驻，否则公网会
返回 502。先完成依赖安装和前端构建，再创建以下两个单元。

### API 服务

使用 `sudoedit /etc/systemd/system/trading-codex-api.service` 创建：

```ini
[Unit]
Description=Trading Codex API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=radxa
Group=radxa
WorkingDirectory=/home/radxa/quant/trading-codex
Environment=TRADING_CODEX_ENVIRONMENT=production
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/home/radxa/quant/trading-codex/.venv/bin/uvicorn trading_codex.main:app --app-dir /home/radxa/quant/trading-codex/backend/src --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=/home/radxa/quant/trading-codex/data /home/radxa/quant/trading-codex/artifacts

[Install]
WantedBy=multi-user.target
```

### 前端服务

使用 `sudoedit /etc/systemd/system/trading-codex-web.service` 创建：

```ini
[Unit]
Description=Trading Codex Web
After=network-online.target trading-codex-api.service
Wants=network-online.target trading-codex-api.service
ConditionPathExists=/home/radxa/quant/trading-codex/web/dist/index.html

[Service]
Type=simple
User=radxa
Group=radxa
WorkingDirectory=/home/radxa/quant/trading-codex/web
ExecStart=/home/radxa/.nvm/versions/node/v24.18.0/bin/node /home/radxa/quant/trading-codex/web/node_modules/vite/bin/vite.js preview --host 127.0.0.1 --port 5555 --strictPort
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
```

前端单元中的 Node 路径来自本机当前的 `command -v node`。升级或切换 Node
版本后，应重新检查路径并更新 `ExecStart`。

加载并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now trading-codex-api.service trading-codex-web.service

systemctl --no-pager --full status trading-codex-api.service
systemctl --no-pager --full status trading-codex-web.service
```

部署新代码时，不要用 root 身份运行 `uv` 或 `pnpm`：

```bash
cd /home/radxa/quant/trading-codex

uv sync --frozen
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
sudo systemctl restart trading-codex-api.service trading-codex-web.service
```

当前阶段使用 `vite preview` 适合低流量、单用户并受 Access 保护的个人部署，
但它不是面向公网设计的正式 Web 服务器。应用进入长期实盘使用前，建议改为
Caddy、Nginx 或由后端托管静态构建产物，同时保持 tunnel 和 Access 边界不变。

## 7. 分层排障

按从内到外的顺序检查，不要看到 Cloudflare 错误就直接重启 tunnel：

| 现象 | 优先检查 | 含义或处理 |
| --- | --- | --- |
| `127.0.0.1:8000` 健康检查失败 | `trading-codex-api.service` 日志 | FastAPI 未运行或启动失败 |
| `8000` 正常但 `5555/api/...` 失败 | `trading-codex-web.service` 和 Vite 代理 | 前端进程或代理配置异常 |
| 公网返回 Cloudflare 502 | 先执行本机两个 `curl` | 常见原因是本地 origin 未监听，不一定是 tunnel 断线 |
| 公网返回 Vite 403 Host 错误 | Published route 的 HTTP Host Header | 应设为 `localhost` |
| 公网直接打开应用、没有登录 | Access 应用 hostname/policy | 立即禁用 route，先修复 Access |
| Access 登录成功但 API 显示不可用 | `curl 127.0.0.1:5555/api/v1/system/status` | 后端或 `/api` 代理故障 |
| Tunnel 显示 Degraded | `/ready`、HA 连接数和近期日志 | 单条 QUIC 重连不等于 tunnel 全部中断 |
| 日志出现 `connect: connection refused` | 错误中的 `127.0.0.1:端口` | 对应本地 origin 没有运行 |

常用检查命令：

```bash
curl -fsS http://127.0.0.1:20241/ready

curl -fsS http://127.0.0.1:20241/metrics \
  | rg '^cloudflared_tunnel_(ha_connections|request_errors|total_requests)'

journalctl -u cloudflared --since '30 minutes ago' --no-pager
journalctl -u trading-codex-api --since '30 minutes ago' --no-pager
journalctl -u trading-codex-web --since '30 minutes ago' --no-pager
```

`cloudflared_tunnel_request_errors` 是进程启动以来的累计值。应比较部署前后的
增量，并结合 `/ready`、HA 连接数和 origin 日志判断，不能只凭累计数字判断
当前故障。

### Clash Verge / Mihomo 注意事项

这台设备通过 Clash Verge/Mihomo 外联。若日志显示 `cloudflared` 尝试连接
`198.18.0.x:7844`、DNS 超时或长期无法注册连接，应检查 Fake-IP 映射和规则
顺序。相关域名应在宽泛代理规则之前走 DIRECT：

```yaml
rules:
  - DOMAIN-SUFFIX,argotunnel.com,DIRECT
  - DOMAIN-SUFFIX,cfargotunnel.com,DIRECT
  - DOMAIN-SUFFIX,cftunnel.com,DIRECT
```

使用 Fake-IP 时，还应让这些域名返回真实地址：

```yaml
dns:
  fake-ip-filter:
    - '+.argotunnel.com'
    - '+.cfargotunnel.com'
    - '+.cftunnel.com'
```

`DIRECT` 表示 Mihomo 的策略选择，不代表数据包完全绕过 TUN；
`fake-ip-filter` 和路由规则解决的是两个不同层面的问题。不要在没有当前日志
证据时强制改成 HTTP/2 或反复重启 `cloudflared`。

## 8. 撤回公网发布

需要立即下线时：

1. 在现有 tunnel 中删除或禁用 `trade.example.com` 对应的 Published
   application route。不要删除整个共享 tunnel。
2. 确认公网域名已经无法到达 origin。
3. 再删除或禁用对应的 Access 应用和不再需要的 DNS 记录。
4. 如本地服务也不再需要，执行：

```bash
sudo systemctl disable --now trading-codex-web.service trading-codex-api.service
```

删除 route 是最先执行的止血动作，因为只删除 Access 应用、保留 route 可能
反而让 origin 失去身份验证保护。

## 官方参考

- [创建远程管理的 Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/)
- [添加 Published application route](https://developers.cloudflare.com/cloudflare-one/networks/routes/add-routes/#add-a-published-application-route)
- [发布受 Access 保护的自托管应用](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)
- [Tunnel origin 参数](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/)
- [One-time PIN 登录](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/)

以上 Cloudflare 控制台路径和官方链接于 2026-08-07 核对。控制台导航可能
调整，但安全顺序不变：先建立 Access policy，再发布 route，最后从未认证的
外部网络验证。
