# 灰度实验室对外访问设计

> **文档状态：HISTORICAL。** 本文是内部设计记录，不是当前操作 SOP；当前入口见 [文档中心](../../README.md)。

**日期**: 2026-06-24
**决策**: 采用方案 A，即外部 HTTPS 入口通过反向代理或受控隧道转发到本机 `127.0.0.1:8100`，FastAPI/uvicorn 不直接暴露公网。
**范围**: 让外部用户可通过对外域名访问灰度因子实验室页面和只读 API，同时保留本机部署、安全写库边界和 panda_quantflow iframe 接入能力。

## 背景

当前 Bond Factor Lab 的后端和静态前端由同一个 FastAPI 应用服务，launchd 将 uvicorn 绑定在 `127.0.0.1:8100`。这是适合本机和内网 iframe 接入的安全默认值。对外开放后，不能只把监听地址改成 `0.0.0.0`，因为当前应用存在以下外部访问风险：

- GET 页面和 GET API 默认无登录保护。
- `BOND_ADMIN_TOKEN` 未配置时，管理类 POST 接口 fail-closed 返回 503。
- CORS 目前只配置了 localhost 默认来源，外部域名和 iframe 父页面来源未配置化。
- 当前服务没有明确的 Host 白名单、安全响应头、CSP `frame-ancestors` 和外部访问运行模式。

## 目标

1. 外部用户通过 `https://{public_domain}/` 访问灰度实验室，不直接访问 `:8100`。
2. 本地服务继续绑定 `127.0.0.1:8100`，MySQL、scheduler 和内部端口不对外开放。
3. 外部访问默认只读；所有写库或触发类接口必须 fail-closed。
4. 支持被 panda_quantflow 外层页面跨域 iframe 嵌入，前提是父页面 origin 在 allowlist 中。
5. 所有域名、父页面 origin、公开模式、认证策略通过环境变量或代理配置表达，不把真实域名写死进前端代码。
6. 提供可验证的部署文档和测试覆盖，后续换域名或换代理方式时不用改业务逻辑。

## 非目标

- 不新增方案算法、scheduler 语义或数据库 schema。
- 不把 MySQL、APScheduler 或 harness 管理能力暴露给外部用户。
- 不实现完整多租户权限系统；本阶段只做灰度访问的入口认证和应用级保护。
- 不要求用固定代理产品。Nginx、Caddy、Cloudflare Tunnel、Tailscale Funnel 或公司网关都可以承载同一应用契约。

## 需要用户提供的部署信息

实施前需要确认以下部署值：

1. `public_domain`: 对外域名，例如 `gray-lab.example.com`。
2. `public_url`: 推荐为 `https://gray-lab.example.com/`，且使用根路径服务。
3. `entry_type`: 入口方式，取 `reverse_proxy`、`cloud_tunnel` 或 `company_gateway`。
4. `tls_owner`: TLS 由代理自动签发、公司证书、还是云隧道托管。
5. `reader_auth`: 外部只读访问认证方式，推荐优先级为公司 SSO、VPN/网关白名单、Basic Auth。
6. `admin_access`: 管理接口是否只允许本机访问；推荐 `local_only`。
7. `iframe_parent_origins`: 允许嵌入灰度实验室的父页面 origins，例如 `https://quantflow.example.com`。

这些值属于部署配置，不进入 Git 中的敏感文件。仓库只保存 `.env.example`、文档示例和测试用假域名。

## 推荐架构

```text
External user
  -> https://{public_domain}:443
  -> reverse proxy / tunnel / gateway
     - TLS termination
     - reader authentication
     - optional IP allowlist
     - request size and rate limits
  -> http://127.0.0.1:8100
     - FastAPI API
     - frontend static files
     - admin POST fail-closed in public mode
  -> MySQL localhost:3306
```

`uvicorn` 仍只监听本机回环地址。代理负责公网监听、证书、只读用户登录和基础访问控制。FastAPI 负责应用级 Host 校验、安全响应头、admin fail-closed、CORS 与 iframe allowlist。

## 后端设计

### 运行模式

新增环境变量：

- `BOND_PUBLIC_MODE`: `false` 或 `true`。公网入口启用时设为 `true`。
- `BOND_PUBLIC_BASE_URL`: 对外 URL，例如 `https://gray-lab.example.com`。
- `BOND_ALLOWED_HOSTS`: 逗号分隔 Host 白名单，例如 `gray-lab.example.com,localhost,127.0.0.1`。
- `BOND_CORS_ORIGINS`: 逗号分隔 API 跨域来源。独立同源部署时可只包含 `BOND_PUBLIC_BASE_URL` 和本地开发来源。
- `BOND_FRAME_ANCESTORS`: 逗号分隔 iframe 父页面 origin，用于 CSP `frame-ancestors`。
- `BOND_ALLOWED_PARENT_ORIGINS`: 逗号分隔 postMessage 父页面 origin，供前端校验跨域导航消息。
- `BOND_ADMIN_TOKEN`: 管理类 POST token。必须配置；未配置时管理类 POST 返回 503。

### Host 与代理头

FastAPI 增加 `TrustedHostMiddleware`，只接受 `BOND_ALLOWED_HOSTS` 中的 Host。launchd 的 uvicorn 参数增加 `--proxy-headers` 和受控的 `--forwarded-allow-ips`，只信任本机代理或隧道本地进程。

### 安全响应头

增加轻量 middleware，为所有响应设置：

- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`
- `Content-Security-Policy`，至少包含 `default-src 'self'`、`frame-ancestors` allowlist、`connect-src 'self' {public_url}`。

如果继续使用 Google Fonts CSS，需要 CSP 明确允许 `https://fonts.googleapis.com` 和 `https://fonts.gstatic.com`；更稳妥的后续优化是本地化字体资源。

### 管理类接口

受保护写接口包括：

- `POST /api/schemes/{scheme_id}/trigger`
- `POST /api/admin/registry/sync`

规则：

- 未配置 `BOND_ADMIN_TOKEN` 时，所有管理类 POST fail-closed 返回 503。
- 配置 `BOND_ADMIN_TOKEN` 时，必须携带匹配的 `X-Admin-Token`。
- 代理层也应禁止外部访问 `/api/admin/*` 和 `/api/schemes/*/trigger`；应用层保护作为第二道防线。

### 健康检查

保留 `GET /api/health`。对外部署可让代理访问该路径做本机健康检查；是否对公网用户公开由代理配置决定。公开时只能返回简单状态，不泄露 DB 凭据、表名或内部路径。

## 前端设计

### API 路径

当前前端使用相对路径 `/api/schemes`、`/api/metrics/{scheme_id}` 和 `/api/backtests/factor-lab`。独立域名根路径部署时不需要改 API URL。正式要求是对外服务必须部署在域名根路径；如果未来需要子路径部署，另开一个设计处理 base path。

### 运行时配置

新增只读运行时配置，供前端读取公开状态和允许父页面 origin：

- 后端提供 `GET /api/runtime-config`，返回 `public_base_url`、`environment_label`、`allowed_parent_origins`。
- 前端启动时读取该接口；失败时使用保守默认：环境标签显示 `LOCAL`，只接受同源 postMessage。
- 顶栏环境标签由写死的 `LOCAL` 改为运行时配置值，公网灰度显示 `GRAY` 或部署配置给定的标签。

### iframe 与 postMessage

当前前端只接受同源 `postMessage`。对 panda_quantflow 跨域 iframe 接入，改为：

- 默认允许 `window.location.origin`。
- 额外允许 `allowed_parent_origins` 中的 origin。
- 不在 allowlist 中的消息全部忽略。
- 消息内容仍只接受 `type="aifin:navigate"` 且 route 在本地路由表中存在。

后端 CSP 的 `frame-ancestors` 与前端 postMessage allowlist 必须使用同一批父页面 origins。

## 入口层设计

### 推荐代理行为

代理或隧道负责：

- 监听公网 `443`，终止 TLS。
- 将 `/` 和 `/api/*` 转发到 `http://127.0.0.1:8100`。
- 注入 `X-Forwarded-Proto`、`X-Forwarded-Host` 和 `X-Forwarded-For`。
- 对外部用户执行只读访问认证。
- 阻断外部访问管理类 POST 路径。
- 可选：限制请求速率、IP 白名单、访问日志脱敏。

### 不推荐的行为

- 不开放 `8100` 到公网。
- 不让代理转发到 `0.0.0.0:8100`。
- 不在 URL 中携带 admin token。
- 不让浏览器端 JS 持有 admin token。

## 测试计划

### 单元测试

- `backend.main._cors_origins` 保持兼容本地默认，并覆盖公网域名配置。
- 新增 Host allowlist 配置解析测试。
- 新增 security headers middleware 测试，覆盖 CSP `frame-ancestors`。
- 新增 `BOND_PUBLIC_MODE=true` 且缺少 `BOND_ADMIN_TOKEN` 时管理类 POST fail-closed 测试。
- 新增 `GET /api/runtime-config` 测试，确认不返回敏感 token。
- 前端 Node VM 测试覆盖运行时配置、环境标签和跨域 postMessage allowlist。

### 本地集成验证

- `curl http://127.0.0.1:8100/api/health` 返回 ok。
- 通过本机代理访问 `https://gray-lab.example.test/` 能加载页面和 `/api/backtests/factor-lab`。
- Host 不在 allowlist 时返回 400。
- 外部路径访问管理类 POST 被代理阻断；绕过代理直打应用时也被应用拒绝。
- iframe 父页面 origin 在 allowlist 内时导航消息生效，不在 allowlist 内时无效。

### 回归测试

至少运行：

```bash
python -m pytest tests/test_backend_api.py tests/test_backend_serving.py tests/test_frontend_static_cache.py tests/test_frontend_factor_lab.py -q
```

实施前需要先修正当前已存在的前端静态 cache-buster 测试基线不一致问题，避免旧失败混入本功能验收。

## 文档更新

新增或更新：

- `.env.example`: 增加公网模式相关变量，使用假域名。
- `docs/architecture/ARCHITECTURE.md`: 更新部署拓扑，明确公网入口不直接暴露 uvicorn。
- `docs/CURRENT_STATUS.md`: 记录当前外部访问能力状态。
- 新增 `docs/deploy/EXTERNAL_ACCESS.md`: 写清代理示例、launchd 重载、验证命令、回滚步骤。

## 验收标准

1. 外部用户能通过 HTTPS 域名打开因子实验室并读取真实 API 数据。
2. `127.0.0.1:8100` 仍为本机监听，公网端口扫描看不到 `8100`。
3. 未授权外部用户不能访问页面或 API。
4. `BOND_PUBLIC_MODE=true` 时，缺失或错误 admin token 不能触发预测或 registry sync。
5. Host、CORS、CSP、iframe 父页面 origin 都由配置控制。
6. panda_quantflow iframe 嵌入时，允许来源可导航；未知来源消息被忽略。
7. 自动化测试覆盖新增安全边界，并保留现有前端/API 数据口径。

## 后续实施切分

1. 应用安全配置与后端测试。
2. 前端 runtime-config 与 postMessage allowlist。
3. launchd 和代理部署文档。
4. 本机代理集成验证。
5. 真实域名接入、外部访问 smoke test 和回滚演练。
