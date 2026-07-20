# PRD：本地灰度实验室外网只读访问

**文档状态**：`HISTORICAL`

**目标读者**：产品、平台和安全审计人员

**最后核验日期**：2026-06-28

> 本文是公网只读访问的时点需求记录，不是当前部署或运维 SOP。

**日期**: 2026-06-28
**阶段**: 本地服务外网只读访问
**目标地址**: `https://bond.finailab.cn/bond-factor-lab/`
**开发分支**: `codex/audit-bugfixes-20260613`

## 1. 背景

当前 Bond Factor Lab 灰度实验室运行在本地 Mac，后端与前端由 FastAPI 服务在 `127.0.0.1:8100`。客户需要从外网访问灰度实验室前端，用于查看现有入库方案的回测与实盘结果展示。

本阶段只解决“本地服务被外网**只读展示**访问”这一件事，不改变数据库、算法环境、scheduler 或方案运行链路。

## 2. 目标

为本地运行的灰度实验室提供一个稳定的公网 HTTPS **只读展示**访问路径，并将外部请求安全转发到本地服务。

目标访问路径：

```text
https://bond.finailab.cn/bond-factor-lab/
```

只读展示的明确含义：

- 客户**只能看到前端渲染后的结果页面**：现有入库方案的状态、各方案的回测结果与实盘结果，二者展示形式一致。
- 客户**不能**通过公网入口获取展示页面之外的任何接口数据（原始预测/实际值导出、回测明细、运行日志、注册表管理等一律不暴露）。
- 客户**不能**触发任何写库、触发预测或管理类操作。

## 3. 非目标

- 不迁移数据库。
- 不迁移算法环境或 scheduler。
- 不修改 `schemes/` 下任何算法方案、core、benchmark 或 config。
- 不把 `bond.finailab.cn` 根路径完全交给灰度实验室；灰度实验室只挂载在 `/bond-factor-lab/`。
- 不开放公网写库、手动触发预测或任何管理接口。
- 不向公网开放“展示页面不需要”的只读数据导出接口（见 §6 拒绝清单）。
- 不为外部用户提供账号、登录、个性化或交互式查询能力；公网侧只有匿名只读浏览。

## 4. 用户场景

1. 客户在外网浏览器打开 `https://bond.finailab.cn/bond-factor-lab/`，看到灰度实验室前端结果页面。
2. 客户在页面中查看每个方案当前入库情况，以及各方案的回测结果与实盘结果（展示一致）。
3. 客户尝试直接访问展示页面之外的任何 API（原始数据导出、admin、trigger）时，被公网入口拦截为 `403`。
4. 本地 Mac 仍可通过 `http://127.0.0.1:8100/` 原方式完整访问、调试和管理。

## 5. 方案概述

```text
外网用户
  -> https://bond.finailab.cn/bond-factor-lab/
  -> 公网 HTTPS 入口（反向代理 + 默认拒绝的访问控制）
  -> SSH 反向隧道
  -> 本地 Mac 127.0.0.1:8100（FastAPI：前端静态 + 只读展示 API）
```

本地 Mac 仍是实际应用、数据库和算法服务所在机器。公网入口只负责 HTTPS、路径转发，以及**默认拒绝、按展示白名单放行**的访问控制。

## 6. 访问控制模型（本阶段核心）

公网入口采用**默认拒绝（default-deny）**：除显式白名单外的一切路径返回 `403`。白名单只包含“渲染结果页面所必需的最小集合”。

### 6.1 公网允许（展示白名单）

| 类型 | 路径 | 用途 |
|------|------|------|
| 静态 | `/bond-factor-lab/`、`/bond-factor-lab/index.html`、`/bond-factor-lab/assets/*`、`*.css`、`*.js` | 前端页面与静态资源 |
| 只读 API | `GET /bond-factor-lab/api/schemes` | 方案列表与状态 |
| 只读 API | `GET /bond-factor-lab/api/metrics/{scheme_id}` | 单方案准确率/指标 |
| 只读 API | `GET /bond-factor-lab/api/backtests/factor-lab` | 回测 + 实盘结果展示数据 |
| 健康检查 | `GET /bond-factor-lab/api/health` | 外部监控/告警 |

> 上述 3 个只读 API 是当前前端 `frontend/aifin-shell.js` 实际渲染所依赖的全部数据接口；公网放行集合即等于前端渲染所需的最小集合。

### 6.2 公网拒绝（一律 403）

| 类型 | 路径 | 拒绝原因 |
|------|------|----------|
| 写/触发 | `POST /bond-factor-lab/api/schemes/{scheme_id}/trigger` | 触发预测，写库类操作 |
| 写/管理 | `POST /bond-factor-lab/api/admin/registry/sync` | 注册表同步，管理类操作 |
| 原始数据导出 | `GET .../api/predictions` | 原始逐条预测，非展示所需 |
| 原始数据导出 | `GET .../api/actuals` | 原始实际值，非展示所需 |
| 原始数据导出 | `GET .../api/targets` | 标的注册表，非展示所需 |
| 原始数据导出 | `GET .../api/backtests/runs`、`.../runs/{id}`、`.../runs/{id}/diffs`、`.../data-checks` | 回测运行明细/差异/数据校验，非展示所需 |
| 其它 | 任何未在 §6.1 白名单中的路径 | 默认拒绝 |

### 6.3 关于“不能获取接口数据”的边界说明（必须如实告知）

前端为客户端渲染（client-rendered），§6.1 中 3 个只读 API 返回的 JSON，会被用户自己的浏览器请求以渲染页面，因此这部分 JSON **在浏览器开发者工具中天然可见**，技术上无法对“正在展示的结果”本身做加密隐藏。

因此本阶段“不能通过前端获取接口数据”的准确含义是：

- 公网暴露面被收敛为**仅展示所需的最小只读集合**；
- **没有**任何原始数据批量导出/管理/写库接口对公网开放；
- 用户能取得的，至多等于页面上已经展示给他看的结果，**不会多于屏幕上的内容**。

如需进一步隐藏底层数据结构（例如改为服务端渲染或字段裁剪/聚合输出），属于后续增强，不在本阶段范围。

## 7. 功能需求

| 编号 | 需求 |
|------|------|
| R1 | `bond.finailab.cn` 可被外网解析并访问。 |
| R2 | 公网入口支持 `/bond-factor-lab/` 路径前缀访问，并将其转发到本地 `127.0.0.1:8100`。 |
| R3 | 页面在 `/bond-factor-lab/` 下运行时，可展示每个方案的入库状态、回测结果与实盘结果。 |
| R4 | HTTP 自动 301 跳转 HTTPS。 |
| R5 | `GET /bond-factor-lab/api/health` 返回 `{"status":"ok"}`。 |
| R6 | 公网入口默认拒绝：只放行 §6.1 白名单，其余路径（含 §6.2）一律 `403`。 |
| R7 | 写/触发/管理接口（trigger、admin/registry/sync）公网 `403`；本地后端另设 `BOND_ADMIN_TOKEN` 作为第二道闸。 |
| R8 | 展示所需之外的只读数据导出接口（predictions、actuals、targets、backtests/runs* 、data-checks）公网 `403`。 |
| R9 | 本地 Mac 经 `127.0.0.1:8100` 的完整访问与管理能力不受公网访问控制影响。 |

## 8. 验收标准

### 8.1 公网行为

```text
# 允许（展示白名单）
GET  https://bond.finailab.cn/bond-factor-lab/                         -> 200 （前端页面）
GET  https://bond.finailab.cn/bond-factor-lab/api/health               -> 200 {"status":"ok"}
GET  https://bond.finailab.cn/bond-factor-lab/api/schemes              -> 200
GET  https://bond.finailab.cn/bond-factor-lab/api/metrics/{scheme_id}  -> 200
GET  https://bond.finailab.cn/bond-factor-lab/api/backtests/factor-lab -> 200

# 拒绝（写/管理）
POST https://bond.finailab.cn/bond-factor-lab/api/schemes/{id}/trigger -> 403
POST https://bond.finailab.cn/bond-factor-lab/api/admin/registry/sync  -> 403

# 拒绝（原始数据导出）
GET  https://bond.finailab.cn/bond-factor-lab/api/predictions          -> 403
GET  https://bond.finailab.cn/bond-factor-lab/api/actuals              -> 403
GET  https://bond.finailab.cn/bond-factor-lab/api/targets              -> 403
GET  https://bond.finailab.cn/bond-factor-lab/api/backtests/runs       -> 403
GET  https://bond.finailab.cn/bond-factor-lab/api/backtests/data-checks-> 403

# HTTP -> HTTPS
GET  http://bond.finailab.cn/bond-factor-lab/api/health                -> 301 跳转 HTTPS
```

### 8.2 本地不受影响

```text
GET  http://127.0.0.1:8100/                  -> 200
GET  http://127.0.0.1:8100/api/predictions   -> 200 （本地仍可访问）
```

### 8.3 前端回归测试

```bash
conda run -n bond_factor_lab_service python -m unittest tests.test_frontend_factor_lab
```

预期结果：全部通过。

## 9. 当前状态

已完成：

- 域名公网访问路径已配置。
- HTTP / HTTPS 公网入口已可用，HTTPS 证书已生效。
- 本地 Mac 到公网入口的 SSH 反向隧道可用（手动）。
- 前端已支持 `/bond-factor-lab` 路径前缀（`frontend/aifin-shell.js`）。
- admin / trigger 公网入口已被拦截。

交付物已落仓库（`deploy/`、`scripts/`，待部署+验收）：

- 公网入口访问控制从“仅拦截 admin/trigger”收敛为 §6 的**默认拒绝 + 展示白名单**：`deploy/nginx/bond-factor-lab.conf` + `deploy/nginx/snippets/bond-proxy-headers.conf`（补齐 R6/R8：原始数据导出接口公网 403）。
- 公网入口配置全部纳入 `deploy/` 版本管理（见 §11 决策）。
- SSH 反向隧道改为 `launchd` 常驻：`deploy/launchd/com.bond-factor-lab.ssh-tunnel.plist`（ssh + KeepAlive）。
- 后端 `BOND_ADMIN_TOKEN` 第二道闸（R7）：`deploy/launchd/com.bond-factor-lab.backend.plist` 占位符。
- 验收脚本 `scripts/check_public_access.sh`（200/403 矩阵）+ 监控模板 `scripts/healthcheck_alert.sh`。
- 部署/回滚说明 `deploy/README.md`。

待用户执行（无法在开发会话内完成）：

- 把 `deploy/nginx/*` 部署到入口机并 `nginx -t && reload`；把隧道/后端 plist 加载到本地 Mac；填入真实 `<SSH_USER>`/`<TUNNEL_KEY>`/`BOND_ADMIN_TOKEN`。
- 在能联公网的机器上跑 `scripts/check_public_access.sh` 完成验收。

## 10. 风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 本地 Mac 睡眠或断网 | 外网访问不可用 | 用 `launchd` 托管隧道，并关闭睡眠或设置唤醒策略 |
| SSH 隧道断开 | 公网入口无法转发到本地服务 | 增加隧道守护和健康检查 |
| 本地后端卡住 | 前端和 API 超时 | 保留本地 health 检查和 launchd 后端重启机制 |
| 访问控制只拦 admin 未拦数据导出 | 客户可绕过页面直接拉原始数据 | 改为默认拒绝 + 展示白名单（R6/R8），其余路径 403 |
| admin 接口暴露 | 可能触发写库操作 | 本地 `BOND_ADMIN_TOKEN` + 公网入口 403 双保险 |
| 展示 JSON 在浏览器可见 | 客户可读到页面已展示数据的原始 JSON | 已在 §6.3 如实说明；如需更严格，后续改服务端渲染/字段裁剪 |

## 11. 决策记录（已确认）

| 问题 | 决策 | 影响 |
|------|------|------|
| 公网入口反代/访问控制配置是否纳入仓库 | **纳入**。本仓库为私有仓库，公网入口配置（nginx/caddy 等反代 + §6 访问控制规则）**全部纳入** `deploy/` 留档，**不做脱敏**（含真实域名与规则细节）。 | 新增交付物：`deploy/` 下入口配置文件，纳入版本管理，便于复现与审计。 |
| §6.3 展示 JSON 在浏览器可见是否可接受 | **可接受**。本阶段只在公网入口做“默认拒绝 + 展示白名单”的配置层收口，**不改后端**（不做服务端渲染/字段裁剪）。 | 工作量收敛在入口配置层；后端、前端、算法链路零改动。 |
| 健康检查 `/api/health` 是否公网开放 | **公网开放**。保留 R5，允许外部监控直接探活，仅返回 `{"status":"ok"}`，不含方案数据。 | health 留在 §6.1 白名单。 |

> 说明：公网入口配置“不脱敏、全部纳入”仅适用于本私有仓库；若仓库可见性变化（转公开/外发），须先评估域名与规则泄露风险。
