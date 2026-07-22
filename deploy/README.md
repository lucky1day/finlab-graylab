# 部署：本地灰度实验室外网只读访问

当前公网性能与访问控制规范以
[`docs/superpowers/specs/2026-07-22-factor-lab-subsecond-dashboard-design.md`](../docs/superpowers/specs/2026-07-22-factor-lab-subsecond-dashboard-design.md)
为准。Task 10 将把可执行性能验收手册落到
[公网性能运行手册](../docs/operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md)，并同步
`docs/operations/README.md` 入口。这是实施计划中的有意顺序依赖：Task 10 手册落地前禁止执行本次发布。
历史公网 PRD 只作决策记录，不能覆盖当前规范。

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
| 公网入口机 | Nginx 443 入口 + 访问控制 | `deploy/nginx/bond-factor-lab.conf`、`deploy/nginx/snippets/bond-proxy-headers.conf`（部署为版本化片段） |
| 本地 Mac | 后端、scheduler、SSH 反向隧道 | `deploy/launchd/*.plist` |
| 任意可联公网点 | 验收 / 监控 | `scripts/check_public_access.sh`、`scripts/healthcheck_alert.sh` |

## 当前运维基线

2026-07-21 复审后的本地运行基线：

- backend 和 scheduler 均由 launchd 管理，服务端口仍为 `127.0.0.1:8100`。
- scheduler 调整配置、方案 `config.yaml` 或代码后，必须 `launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.scheduler` 重启，并复核日志中 active 日频、周频、周平均、月频方案均已注册。
- scheduler 的 launchd 配置使用 `RunAtLoad=true` 与 `KeepAlive=true`：Mac 登录该用户会自动拉起，进程退出会被 launchd 重新拉起。若需要无人登录前启动，应另行制作 root `LaunchDaemon`，不能直接复用当前依赖用户 conda 环境的 `LaunchAgent`。
- Blackbox V2 专用 preflight 每天 `06:00` 全量刷新 DataBridge 三频 current，`06:30` 检查，未通过则 `06:35` 条件重刷，`07:00` 最终校验；成功后才重启 scheduler，失败只阻断 V2 且不补跑。Native V1 不读取该 Gate。DataBridge 凭据只保存在本机 `.env`，不得写入 plist 或仓库。
- scheduler 启动后会执行一次 startup catch-up：对当天业务 cron 已过、且 `t_scheme_runs` 尚无终态记录的 active 方案自动补跑，避免系统/服务在早间预测窗口之后恢复时静默缺当天预测。
- actuals 每天 `08:30/19:00/23:45` 三档刷新；`23:45` 用于承接 BondPrediction `23:25` 左右的 Wind 日频导入。若源表在最后一档之后才补齐，非交易日 actuals job 会补刷上一交易日的 daily/weekly actual，避免前端 T+1 最新验证卡在上一交易日。
- 慢速 source-backed 方案使用 `config.yaml.schedule.timeout_sec` 配置方案级 executor timeout，例如 10Y02 当前为 `3600` 秒；该配置只影响算法子进程等待预算，不改变业务 cron 或日期语义。

## 访问控制（默认拒绝 + 精确展示白名单）

Nginx 负责公网精确白名单、原始 URI guard、限流和短超时；后端负责一致性
dashboard、健康状态、`Vary` 和隧道前 gzip。两层合同必须同时验收。

- **始终放行（GET/HEAD）**：`/bond-factor-lab/`、必要的精确 HTML/CSS/JS/两个 SVG、`/api/health`、`/api/factor-lab/dashboard`。
- **rollout 临时放行（GET/HEAD）**：旧 `/api/schemes`、`/api/metrics/{id}`、`/api/backtests/factor-lab`，供新旧前端兼容。
- **final 拒绝**：上述三个旧展示 API 与其它所有页面/API。FastAPI 本机旧路由仍保留用于 harness 和回滚。
- 原始 path 中任何 `%HH`、双斜杠、明文单/双 dot segment、大小写和尾斜杠变体均 fail closed；query 不由 path guard 误杀，而由各只读 API 自身契约校验；写接口始终拒绝。

> Nginx location 优先级细节见 `deploy/nginx/bond-factor-lab.conf` 顶部注释。

## 部署步骤

### 1) 公网入口机：Nginx

<!-- NGINX_RELEASE_SCRIPT_BEGIN -->
```bash
publish_bond_factor_nginx() (
  set -euo pipefail

  release_stage="${BOND_FACTOR_RELEASE_STAGE:-rollout}"
  release_id="20260722b"
  nginx_root="${BOND_NGINX_ROOT:-/etc/nginx}"
  case "$release_stage" in
    rollout|final) ;;
    *) printf 'invalid BOND_FACTOR_RELEASE_STAGE\n' >&2; return 2 ;;
  esac

  site_source="deploy/nginx/bond-factor-lab.conf"
  snippet_source="deploy/nginx/snippets/bond-proxy-headers.conf"
  snippet_target="${nginx_root}/snippets/bond-proxy-headers-${release_id}.conf"
  target="${nginx_root}/sites-available/bond-factor-lab-${release_id}-${release_stage}"
  active="${nginx_root}/sites-enabled/bond-factor-lab"
  # 生产默认 snippet_target：
  # /etc/nginx/snippets/bond-proxy-headers-20260722b.conf

  candidate_file="$(mktemp)"
  trap 'rm -f -- "$candidate_file"' EXIT
  sed "s/^    default rollout;$/    default ${release_stage};/" \
    "$site_source" >"$candidate_file"
  test "$(grep -Ec '^    default (rollout|final);$' "$candidate_file")" -eq 1
  grep -Fxq "    default ${release_stage};" "$candidate_file"

  install_immutable() {
    source_file="$1"
    immutable_target="$2"
    staged_target="${immutable_target}.stage.$$"
    if sudo test -L "$immutable_target"; then
      printf 'immutable release path must not be a symlink: %s\n' \
        "$immutable_target" >&2
      return 1
    fi
    if sudo test -e "$immutable_target"; then
      if sudo test -f "$immutable_target" \
          && sudo cmp -s "$source_file" "$immutable_target"; then
        return 0
      fi
      printf 'immutable release id collision: %s\n' "$immutable_target" >&2
      return 1
    fi

    sudo install -m 0644 "$source_file" "$staged_target"
    if sudo ln "$staged_target" "$immutable_target" 2>/dev/null; then
      sudo rm -f -- "$staged_target"
      return 0
    fi
    sudo rm -f -- "$staged_target"
    if sudo test ! -L "$immutable_target" \
        && sudo test -f "$immutable_target" \
        && sudo cmp -s "$source_file" "$immutable_target"; then
      return 0
    fi
    printf 'immutable release id collision: %s\n' "$immutable_target" >&2
    return 1
  }

  previous_target=""
  already_active=0
  if sudo test -L "$active"; then
    current_target="$(readlink -f "$active" 2>/dev/null || true)"
    if [[ -z "$current_target" ]] || ! sudo test -f "$current_target" \
        || ! sudo test -r "$current_target"; then
      printf 'active symlink target is missing or unreadable: %s\n' "$active" >&2
      return 1
    fi
    if [[ "$current_target" == "$target" ]]; then
      already_active=1
    else
      previous_target="$current_target"
    fi
  elif sudo test -e "$active"; then
    printf '%s: %s\n' \
      'active path is a regular file; migrate it to an immutable release' \
      "$active" >&2
    return 1
  fi

  install_immutable "$snippet_source" "$snippet_target"
  install_immutable "$candidate_file" "$target"

  if [[ "$already_active" -eq 1 ]]; then
    sudo nginx -t
    printf 'release already active: %s\n' "$target"
    return 0
  fi
  if [[ -n "$previous_target" && "$previous_target" == "$target" ]]; then
    printf 'previous target must differ from candidate\n' >&2
    return 1
  fi

  restore_previous_link() {
    if [[ -n "$previous_target" ]]; then
      rollback_link="${active}.rollback.$$"
      sudo ln -s "$previous_target" "$rollback_link"
      sudo mv -f "$rollback_link" "$active"
    else
      sudo rm -f -- "$active"
    fi
  }

  next_link="${active}.next.$$"
  sudo ln -s "$target" "$next_link"
  sudo mv -f "$next_link" "$active"

  if ! sudo nginx -t; then
    restore_previous_link
    sudo nginx -t
    return 1
  fi
  if ! sudo systemctl reload nginx; then
    restore_previous_link
    if [[ -n "$previous_target" ]]; then
      # previous site 引用其自身不可变 snippet；test 后恢复完整旧策略。
      sudo nginx -t && sudo systemctl reload nginx
    else
      # 首次启用无 previous：删除 active 后只 test，绝不 reload “无站点”。
      sudo nginx -t
    fi
    printf 'candidate reload failed; previous policy restored\n' >&2
    return 1
  fi
)

publish_bond_factor_nginx
```
<!-- NGINX_RELEASE_SCRIPT_END -->

切换 final 使用完全相同的命令块，并设置 `BOND_FACTOR_RELEASE_STAGE=final`；候选由同一
`20260722b` release 模板生成，`nginx -t` 成功后才 reload。不得现场删除或注释 legacy
location。仓库 site 文件不包含 `events {}` / `http {}`，因此不能执行
`nginx -t -c deploy/nginx/bond-factor-lab.conf`；Task 12 必须在入口机真实完整配置中验证。
候选 `nginx -t` 或 reload 失败也必须以非零状态结束，不能因 previous policy 恢复
成功而把失败发布报告为成功。

入口最低支持版本为 Nginx 1.18+。Task 12 必须记录入口机实际 `nginx -v`，并在
真实完整配置执行 `nginx -t` 后才允许 reload。本地 wrapper 不能替代生产入口验证；
它只验证仓库模板在本机 Nginx 上的语法和 location/normalize 行为。

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

### 2a) Bond Project Pro 测试站反向隧道（可选）

`deploy/launchd/com.bondprojectpro.ssh-tunnel.plist` 与 `deploy/nginx/bondprojectpro-test*.conf`
用于 `https://test.finailab.cn/` 测试入口：

- 公网 Nginx 使用 `deploy/nginx/bondprojectpro-test-acme.conf` 完成 ACME 首次签证；证书签发后切换为 `deploy/nginx/bondprojectpro-test.conf`。
- 本地 Mac 使用 `deploy/launchd/com.bondprojectpro.ssh-tunnel.plist` 常驻 SSH 反向隧道，把公网机 `127.0.0.1:18080` 转发到本地 `127.0.0.1:80`。
- 该 plist 只记录 key 路径，不包含私钥内容；实际部署前仍需确认 key 文件权限、远端账号与端口占用。

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
- `BOND_SCHEDULER_STARTUP_CATCHUP=1`：scheduler 启动后补跑当天已错过且没有终态 run 的预测任务；已存在 `success/partial/failed/skipped` run 的方案不会因重启反复补跑。
- `config.yaml.schedule.timeout_sec`：单方案 executor timeout 覆盖值，用于慢速 source-backed 方案；没有配置时使用 executor 默认预算。

调整参数后需要重启 scheduler：

```bash
cp deploy/launchd/com.bond-factor-lab.scheduler.plist ~/Library/LaunchAgents/
launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.scheduler
```

错峰只改变物理启动时间，不改变 `predict_date`、`feature_date`、`target_date` 或方案 `config.yaml` 中的业务基准 cron。

## 验收

```bash
# rollout：dashboard + 旧三类展示 API 均可达
scripts/check_public_access.sh --mode rollout https://bond.finailab.cn

# final：旧三类展示 API 已拒绝；其余协议/安全矩阵不变
scripts/check_public_access.sh --mode final https://bond.finailab.cn
# -> 退出码 0；每项输出 code/size/time

# 前端回归（本地）
conda run -n bond_factor_lab_service python -m unittest tests.test_frontend_factor_lab

# 本地不受影响（仅公网被收口）
curl -s http://127.0.0.1:8100/api/predictions   # 仍 200
```

## 监控（轻量，可选）

外部 uptime 服务直接探 `https://bond.finailab.cn/bond-factor-lab/api/health`；
或用 `scripts/healthcheck_alert.sh` 接入告警通道，cron/timer 周期调度（脚本顶部有示例）。

本地生产数据巡检可使用只读脚本：

```bash
set -a; source .env; set +a
conda run -n bond_factor_lab_service python scripts/check_production_daily_health.py --predict-date "$(date +%F)"
```

退出码约定：`0=ok`，`1=warning`，`2=error`。交易日日频预测整体缺失且不能由源表水位全阻塞解释时会报 error；actual 晚于源表水位会报 error；部分 active 方案没有成功 run 默认报 warning，避免 source-backed 方案 fail-closed 时诱导人工补写。对已知日频方案族，输出会同时列出缺 run 对应的 source watermark 阻塞期限，例如 `expected_feature_date=2026-07-03` 但 `1Y/3Y/5Y/7Y source_max=2026-07-01`。验收窗口可追加 `--strict-runs` 将缺成功 run 升级为 error。

巡检脚本还会检查两类生产一致性问题：

- successful run 的 `records_written` 必须能按 `run_id` 对上 `t_scheme_predictions` 明细，否则报 `successful_run_prediction_rows_mismatch` error。
- active 日频 prediction 的 `predict_date/feature_date/target_date` 必须符合平台 live 日期语义，否则报 `daily_prediction_date_semantics_mismatch` error。

若某交易日所有 active 日频方案都因源表水位早于 expected feature date 而没有有效预测，`daily_predictions_missing` 会降级为 warning，并在 `daily_run_input_watermark_blocked` 中列出阻塞期限；这是外部数据阻塞，不应人工伪造预测。

## 回滚

常规版本回滚必须保持公网可用：SSH 反向隧道和服务必须保持在线，不得用停隧道
代替版本回滚。

```bash
# 1. 入口机先复用上文原子切换块，release_stage=rollout，恢复旧前端依赖 API；
#    nginx -t 和 reload 成功后验证 rollout 矩阵。
scripts/check_public_access.sh --mode rollout https://bond.finailab.cn

# 2. 再按版本化发布流程恢复上一份前端/后端 commit；仅在应用代码需要时
#    kickstart backend，不能 bootout SSH tunnel。
# 3. 最后复核 direct URL、iframe、health、旧展示 API 和写接口拒绝矩阵。
```

不要把 admin token 删除当作版本回滚；未配置 token 时 admin/trigger 写接口会
fail-closed 返回 503。

## 全站紧急下线（造成中断，需专项授权）

只有明确要求全站中断并取得专项授权后，才允许停止 SSH 反向隧道。该操作不属于
本次性能发布的常规回滚：

```bash
launchctl bootout gui/$(id -u)/com.bond-factor-lab.ssh-tunnel
```

## 安全约束

- 真实 `BOND_ADMIN_TOKEN`、SSH 私钥**不入库**（仓库内只放占位符）。
- 隧道远端绑 `127.0.0.1:18100`，公网无法直连裸后端，只有入口机本地 Nginx 可达。
- 展示接口的 JSON 在浏览器天然可见（客户端渲染）；精确白名单只允许当前页面所需的数据面。
