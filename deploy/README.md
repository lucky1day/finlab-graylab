# 部署：本地灰度实验室外网只读访问

落地 PRD [`docs/PRD_PUBLIC_BOND_FACTOR_LAB_ACCESS.md`](../docs/PRD_PUBLIC_BOND_FACTOR_LAB_ACCESS.md)。
本目录是**配置 + 脚本**交付物，**不改任何业务代码**（backend / frontend / schemes / scheduler）。

## 链路

```
https://bond.finailab.cn/bond-factor-lab/
  -> Nginx 443（默认拒绝 + 展示白名单 + 前缀剥离）        [公网入口机]
  -> proxy_pass http://127.0.0.1:18100
  -> SSH 反向隧道                                          [本地 Mac launchd 常驻]
  -> 本地 Mac 127.0.0.1:8100（FastAPI：前端静态 + 只读展示 API）
```

## 三类机器职责

| 机器 | 组件 | 交付物 |
|------|------|--------|
| 公网入口机 | Nginx 443 入口 + 访问控制 | `deploy/nginx/bond-factor-lab.conf`、`deploy/nginx/snippets/bond-proxy-headers.conf` |
| 本地 Mac | 后端、scheduler、SSH 反向隧道 | `deploy/launchd/*.plist` |
| 任意可联公网点 | 验收 / 监控 | `scripts/check_public_access.sh`、`scripts/healthcheck_alert.sh` |

## 当前运维基线

2026-07-05 复审后的本地运行基线：

- backend 和 scheduler 均由 launchd 管理，服务端口仍为 `127.0.0.1:8100`。
- scheduler 调整配置、方案 `config.yaml` 或代码后，必须 `launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.scheduler` 重启，并复核日志中 active 日频、周频、周平均、月频方案均已注册。
- 慢速 source-backed 方案使用 `config.yaml.schedule.timeout_sec` 配置方案级 executor timeout，例如 10Y02 当前为 `3600` 秒；该配置只影响算法子进程等待预算，不改变业务 cron 或日期语义。

## 访问控制（默认拒绝 + 展示白名单）

由 Nginx 入口实现（PRD §6），后端不参与：

- **放行（GET）**：`/bond-factor-lab/`、静态资源、`/api/health`、`/api/schemes`、`/api/metrics/{id}`、`/api/backtests/factor-lab`。
- **403**：其余所有 `/api/*`（`predictions`/`actuals`/`targets`/`backtests/runs*`/`data-checks` 原始数据导出，`schemes/{id}/trigger`、`admin/registry/sync` 写入接口），以及任何非 GET 方法。

> Nginx location 优先级细节见 `deploy/nginx/bond-factor-lab.conf` 顶部注释。

## 部署步骤

### 1) 公网入口机：Nginx

```bash
# 复制配置与公共片段
sudo cp deploy/nginx/bond-factor-lab.conf       /etc/nginx/sites-available/bond-factor-lab
sudo cp deploy/nginx/snippets/bond-proxy-headers.conf /etc/nginx/snippets/
sudo ln -sf /etc/nginx/sites-available/bond-factor-lab /etc/nginx/sites-enabled/bond-factor-lab

# 按入口机实际情况确认：TLS 证书路径、80->443 跳转是否已有（见 conf 注释，二选一）
sudo nginx -t && sudo systemctl reload nginx
```

### 2) 本地 Mac：SSH 反向隧道（launchd 常驻）

```bash
# 先把 plist 里的 <SSH_USER> / <TUNNEL_KEY> 占位符替换为真实值，
# 并确认可免密 ssh 到入口机（接受 host key）。
cp deploy/launchd/com.bond-factor-lab.ssh-tunnel.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.bond-factor-lab.ssh-tunnel.plist

# 状态 / 日志
launchctl print gui/$(id -u)/com.bond-factor-lab.ssh-tunnel
tail -f logs/com.bond-factor-lab.ssh-tunnel.err

# 入口机自检（隧道通了应返回 {"status":"ok"}）
curl -s http://127.0.0.1:18100/api/health
```

### 3) 本地 Mac：后端 admin token（第二道闸，R7）

```bash
# 编辑 deploy/launchd/com.bond-factor-lab.backend.plist，
# 把 BOND_ADMIN_TOKEN 的 __SET_REAL_TOKEN__ 换成随机串（建议不入库）：
openssl rand -hex 24
# 重新安装并重载
cp deploy/launchd/com.bond-factor-lab.backend.plist ~/Library/LaunchAgents/
launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.backend
```

### 4) 本地 Mac：scheduler 错峰与并发

`deploy/launchd/com.bond-factor-lab.scheduler.plist` 默认设置：

- `BOND_SCHEDULER_STAGGER_MINUTES=2`：同一业务 cron 下的 active 方案按稳定顺序每 2 分钟错开启动。
- `BOND_SCHEDULER_PREDICTION_MAX_CONCURRENCY=1`：同一时刻最多 1 个预测方案进入算法子进程，避免多个 `conda run` 同时压机器。
- `config.yaml.schedule.timeout_sec`：单方案 executor timeout 覆盖值，用于慢速 source-backed 方案；没有配置时使用 executor 默认预算。

调整参数后需要重启 scheduler：

```bash
cp deploy/launchd/com.bond-factor-lab.scheduler.plist ~/Library/LaunchAgents/
launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.scheduler
```

错峰只改变物理启动时间，不改变 `predict_date`、`feature_date`、`target_date` 或方案 `config.yaml` 中的业务基准 cron。

## 验收

```bash
# 200/403 矩阵（在能访问公网的机器上运行；覆盖 R3/R4/R5/R6/R8）
bash scripts/check_public_access.sh
# -> 退出码 0；逐项 PASS

# 前端回归（本地，PRD §8.3）
conda run -n bond_factor_lab_service python -m unittest tests.test_frontend_factor_lab

# 本地不受影响（仅公网被收口）
curl -s http://127.0.0.1:8100/api/predictions   # 仍 200
```

## 监控（轻量，可选）

外部 uptime 服务直接探 `https://bond.finailab.cn/bond-factor-lab/api/health`；
或用 `scripts/healthcheck_alert.sh` 接入告警通道，cron/timer 周期调度（脚本顶部有示例）。

## 回滚

```bash
# 入口机：摘掉 Nginx 站点
sudo rm -f /etc/nginx/sites-enabled/bond-factor-lab
sudo nginx -t && sudo systemctl reload nginx

# 本地 Mac：停隧道
launchctl bootout gui/$(id -u)/com.bond-factor-lab.ssh-tunnel

# 本地 Mac：撤销 admin token（删 plist 里 BOND_ADMIN_TOKEN 后 kickstart -k 重载）
```

## 安全约束

- 真实 `BOND_ADMIN_TOKEN`、SSH 私钥**不入库**（仓库内只放占位符）。
- 隧道远端绑 `127.0.0.1:18100`，公网无法直连裸后端，只有入口机本地 Nginx 可达。
- 展示接口的 JSON 在浏览器天然可见（客户端渲染）；用户能取得的不超过页面已展示内容（PRD §6.3）。
