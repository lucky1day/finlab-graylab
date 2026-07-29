# 部署：本地灰度实验室外网只读访问

当前公网性能与访问控制规范以
[公网性能运行手册](../docs/operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md)和本目录中的
版本化 Nginx 配置为准；发布前、rollout、final 与回滚均须按该手册执行。
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

## 当前状态权威与 operator guard

production 的 migration、rollout、服务加载、epoch、cache、direct authority、逐日完成数、
历史缺口和 occurrence 证据只在[当前状态](../docs/CURRENT_STATUS.md)维护；本 runbook
不复制任何动态值。operator 每次执行前必须读取该页，并以受控只读探针核对现场；
若当前状态列出的任一待切换前置尚未闭合，必须 fail-closed，不得继续本手册的
migration、bootstrap、epoch 或服务启动步骤。

下文只定义稳定的目标合同、步骤顺序和安全不变量，不能单独证明生产已上线。

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

  canonical_existing_directory() {
    local input_directory="$1" resolved_directory
    resolved_directory="$(
      sudo readlink -f -- "$input_directory" 2>/dev/null || true
    )"
    if [[ -z "$resolved_directory" ]] \
        || ! sudo test -d "$resolved_directory"; then
      printf 'required nginx directory is missing: %s\n' \
        "$input_directory" >&2
      return 1
    fi
    printf '%s\n' "$resolved_directory"
  }

  snippets_directory="$(
    canonical_existing_directory "${nginx_root}/snippets"
  )"
  sites_available_directory="$(
    canonical_existing_directory "${nginx_root}/sites-available"
  )"
  sites_enabled_directory="$(
    canonical_existing_directory "${nginx_root}/sites-enabled"
  )"

  site_source="deploy/nginx/bond-factor-lab.conf"
  snippet_source="deploy/nginx/snippets/bond-proxy-headers.conf"
  # 候选文件可能尚不存在：先 canonicalize 已存在的父目录，再拼接 basename。
  snippet_target="${snippets_directory}/bond-proxy-headers-${release_id}.conf"
  target="${sites_available_directory}/bond-factor-lab-${release_id}-${release_stage}"
  active="${sites_enabled_directory}/bond-factor-lab"
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
    current_target="$(sudo readlink -f -- "$active" 2>/dev/null || true)"
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

### 4) 本地 Mac：日频 coordinator 待切换 rollout

已经批准的日频目标机器 policy 是 `deploy/daily_scheduler_policy_v2.json`：1 个
coordinator、每交易日 1 个 occurrence、25 个 base execution（17 Native + 8
Blackbox V2）、Native 最大并发 2、V2 最大并发 2，最终必须有 29/29 target
receipt。日常运行使用 warm cache；部署流程不增加额外的性能准入层或双重签名
信任链。以下步骤在专项授权前不得执行；它们不是当前 production 安装态说明。

#### 已安装服务与私有配置

- **已安装的** `com.bond-factor-lab.backend`、
  `com.bond-factor-lab.scheduler` 和 `com.bond-factor-lab.v2-preflight` 三份 plist
  必须同时显式设置 `BOND_DAILY_COORDINATOR_MODE=ledger`；仓库 rollout 文件只
  用于安全 bootstrap，不能覆盖 machine-global epoch。
- ledger 下 `com.bond-factor-lab.v2-preflight` 必须保持 bootout。DataBridge refresh
  由 coordinator 唯一拥有；不得保留第二个 calendar trigger 或 per-scheme daily
  cron。
- `BFL_SOURCE_DB_CONFIG_ROOT=/Users/macstudio0/.config/bond-factor-lab` 与
  `BFL_SOURCE_DB_CONFIG_PATH=/Users/macstudio0/.config/bond-factor-lab/source-runtime-db.json`
  只定位 source-readonly 配置，不得把用户名、密码或 DSN 写入 plist。安装前核验：

  ```bash
  chmod 700 /Users/macstudio0/.config/bond-factor-lab
  chmod 600 /Users/macstudio0/.config/bond-factor-lab/source-runtime-db.json
  ```

  owner、mode、inode、symlink 或 `SHOW GRANTS FOR CURRENT_USER()` 审计失败时必须在
  Registry 同步和算法启动前 fail-closed。只允许直接 `USAGE/SELECT` 和必需源表
  零行 SELECT；不得执行生产 DDL/DML。该配置只属于 Bond Factor Lab，
  不得修改或重启 BondProjectPro。

#### 安全存储与 cache

维护窗口内以实际 scheduler 服务 UID 核验以下 root 及其全部祖先：

- `~/Library/Application Support/BondFactorLab/daily-runtime-v1` 及
  `occurrence-locks`；
- `backtest_artifacts/input_generations/native`；
- `backtest_artifacts/input_generations/databridge`；
- `data/data_bridge` 与 `backtest_artifacts/data_bridge_refresh`；
- `backtest_artifacts/runtime_cache/liwei_0616`。

root 必须是绝对 canonical path、owner 正确、无 symlink，目录为 `0700` 或更严，
终端文件为 `0600/0400`；runtime 不静默 `chmod` 不安全的既有路径。Native、
DataBridge 和 Liwei generation 每次打开都重算 manifest/payload SHA-256，并复核
business/feature date、schema、parent lineage 与 current pointer。

Liwei schema 3 的 exact family/spec/publisher/consumer 以
[日频信号 SLA](../docs/architecture/DAILY_SIGNAL_SLA.md)为准。publisher 在 family
锁内原子发布；consumer 只允许 validated `hit`，不得训练、stage、publish、切换
pointer 或清理 generation。覆盖不足返回 `CACHE_PUBLISHER_REQUIRED`。日常只接受
可证明的 `hit/append/suffix`；失败时 current 保持不变。

#### Canonical migration runner

writer/refresh 全停并完成一致性备份后，只能用 caller-supplied `Engine` 的
`migrations.runner` 和唯一 wrapper `scripts/apply_migrations.py`。先从只读 inspect
JSON 或受控只读 identity query 取得 database name/server UUID；生产值、DSN 和
凭据不得进入文档、报告或 shell history。

普通 apply：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/apply_migrations.py --apply \
  --expected-database-name <database-name> \
  --expected-server-uuid <server-uuid>
```

017 留在 `APPLYING` 时先只读 inspect，再用同一份输出的 digest recovery：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/apply_migrations.py --inspect-applying-017

conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/apply_migrations.py \
    --recover-applying-017 --apply \
    --state-digest <inspect-json中的64位state_digest> \
    --expected-database-name <database-name> \
    --expected-server-uuid <server-uuid>
```

只接受 `COMPLETE` 或 `COMPATIBLE_PARTIAL`；`UNSAFE` 没有自动恢复路径。recovery
必须在 owner lock 内重读并 exact compare，任何状态漂移都拒绝。恢复 017 后必须另行执行普通
`--apply`，才会推进 pending 018。

018 使用同样协议：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/apply_migrations.py --inspect-applying-018

conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/apply_migrations.py \
    --recover-applying-018 --apply \
    --state-digest <inspect-json中的64位state_digest> \
    --expected-database-name <database-name> \
    --expected-server-uuid <server-uuid>
```

018 的闭世界目标是
`t_scheme_runs.started_at=DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6)`；它不增加
composite FK。禁止用 `mysql`、scheduler、harness 或临时脚本直跑 migration SQL。

#### Epoch cutover 与单实例 owner

cutover 代码门禁已由目标 policy/input/cache/result 的直接确定性 authority
承担；runtime、epoch operator 与 cache 路径不得恢复旧 capacity admission
binding、probe 或 qualification。只有测试证明 direct authority 闭合且
[当前状态](../docs/CURRENT_STATUS.md)同步闭合全部前置后，才可执行本节。

切换必须在获准维护窗口一次完成：

1. 阻断 admin/operator recovery，依次 bootout legacy V2 preflight、scheduler 和
   backend；确认 refresh/writer、所有 scheduled-live 和算法进程退出。不能只
   `kickstart -k`。
2. 完成备份与 migration 018 校验后，把**已安装的** backend、scheduler、
   v2-preflight 三份 plist mode 一起改为 `ledger`；v2-preflight 服务继续
   bootout/未加载。用 `plutil`/`PlistBuddy` 逐份读回，服务继续停止。
3. root operator 向 machine-global root-owned append-only epoch chain 只能追加
   更高 epoch。epoch root/`records` 为 root-owned `0755`、record 为 `0644`、
   staging 为 `0700`。发布必须同盘完整写入、`fsync`、逐字节校验，再执行
   hard-link no-clobber 并同步目录；不得覆盖、删除或复用旧 epoch。
4. 使用 canonical operator 发布或确认 ledger epoch：

   ```bash
   sudo install -d -o root -g wheel -m 0755 \
     "/Library/Application Support/BondFactorLab"
   sudo --preserve-env=BOND_DB_USER,BOND_DB_PASSWORD,BOND_DB_HOST,BOND_DB_PORT,BOND_DB_NAME \
     /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
     scripts/daily_coordinator_epoch_operator.py \
       --expected-current-epoch <current-epoch> \
       --mode ledger \
       --transition-id <unique-transition-id> \
       --service-uid "$(id -u macstudio0)" \
       --business-date "$(date +%F)"
   ```

   epoch 发布前，operator 必须按精确 label 核对 backend、scheduler、v2-preflight
   三份 installed plist 全部为 `ledger`，并复核三服务均未加载、quiescence、连续
   epoch、previous digest、canonical JSON、owner/mode 与 service UID 可读性。
   任何失败保持服务停止。
5. 先启动 backend，再只启动一个 scheduler；不要启动 v2-preflight。用
   `launchctl print` 核验 mode，复核 `/api/health.daily_schedule.mode=ledger`、
   machine-global occurrence `flock`、DataBridge refresh owner 和 scheduler PID
   都唯一。

coordinator 的完成权仍由数据库 `current_run_id + attempt_no` fence 决定；旧或
失去完成权的 attempt 不得写 prediction/receipt。`ProcessStartGuard` 必须闭合
`Popen` 到 PID/PGID 登记窗口。重复 tick、重启和 operator recovery 只能复用同一
occurrence，不能创建第二批。

受控回滚也必须先全停并确认全局 quiescence，再把已安装 backend、scheduler、
v2-preflight 三份 plist mode 一起改为 `legacy` 并追加更高 legacy epoch；不得删除
chain、改历史 record、单独改一份 plist 或重放旧 epoch。v2-preflight 是否加载由
目标运行模式另行控制，ledger 下始终保持 bootout。

#### Ledger 运行检查

独立 DataBridge 命令在 ledger 下只允许显式 mode 的只读检查：

```bash
BOND_DAILY_COORDINATOR_MODE=ledger \
  conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/refresh_data_bridge_current.py --check-only \
    --date "$(date +%F)"
```

`--publish`、`--dry-run` 和 `python -m scheduler.main --run-once data-refresh`
都不得成为 ledger 刷新旁路。历史缺口只通过受控 insert-only `gray_live` 补齐。
只有未来交易日真实 coordinator occurrence 具备 occurrence/item/run/receipt 完整
证据时才允许 `scheduled_live`；日期标签本身不构成起点证据。当日终验
必须为 25 winning item、29/29 target receipt、无
duplicate/nonterminal/orphan，且 v2-preflight 保持未加载。

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
conda run -n bond_factor_lab_service python scripts/check_production_daily_health.py \
  --coordinator-mode auto --predict-date "$(date +%F)"
```

退出码约定：`0=ok`，`1=warning`，`2=error`。ledger 模式直接读取冻结
occurrence 的 25 item/29 target 全集，Blackbox V2 不会被排除。08:00 时
28/29 必须返回 error，晚到补齐不改变原 SLA 结果。
legacy 模式保留旧水位诊断，但同样不再从 active daily 查询中过滤 V2。
`auto` 以 scheduler heartbeat 的 mode 为准，并把显式环境当作一致性校验；
已配置 `legacy` 且尚无 heartbeat 时，即使 017 schema 已存在也仍按 legacy
巡检；配置为 `ledger` 却没有 heartbeat、heartbeat mode 非法，或环境与
heartbeat 冲突时返回 error，不能因调用者 shell 缺变量而静默改变模式。

ledger 巡检同时检查 heartbeat、generation、item/target cardinality、缺失
Registry ID、V2 时间线和 write-once SLA。legacy 巡检继续检查：

- successful run 的 `records_written` 必须能按 `run_id` 对上 `t_scheme_predictions` 明细，否则报 `successful_run_prediction_rows_mismatch` error。
- active 日频 prediction 的 `predict_date/feature_date/target_date` 必须符合平台 live 日期语义，否则报 `daily_prediction_date_semantics_mismatch` error。

若 legacy 某交易日所有 active 日频方案都因源表水位早于 expected feature date
而没有有效预测，`daily_predictions_missing` 会降级为 warning，并在
`daily_run_input_watermark_blocked` 中列出阻塞期限；这是外部数据阻塞，
不应人工伪造预测。ledger 不使用该降级代替冻结 generation 证据。

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

machine-global epoch directory **完全不存在**时，尚未进入 epoch 控制，可在
服务全部停止的维护窗口恢复上一份明确为 legacy 的代码/plist；不得留下任一
`ledger` process mode，也不得预建空 epoch directory。

进入 epoch 控制后允许受控回滚，但只能追加更高 epoch：

1. 阻断 admin/operator，bootout 三个 LaunchAgent，并核验无遗留
   scheduler/backend/preflight/算法进程；
2. 所有业务日期的 occurrence/item 必须终态、无 running run、无
   `ABANDONED_FENCE_PENDING_CLEANUP` 和未清孤儿；历史或未来日期遗留也会阻断
   切换。任何旧 ledger occurrence 仍需恢复时禁止改变 epoch，包括
   `ledger → ledger` 维护 epoch；
3. 同时把三份**已安装** plist 改为 `legacy` 并读回；仓库模板与 rollout
   仍保持 `legacy`；
4. 使用同一个 root operator，指定当前 epoch 的精确值，追加
   `N+1/legacy`：

   ```bash
   sudo --preserve-env=BOND_DB_USER,BOND_DB_PASSWORD,BOND_DB_HOST,BOND_DB_PORT,BOND_DB_NAME \
     /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
     scripts/daily_coordinator_epoch_operator.py \
       --expected-current-epoch <N> \
       --mode legacy \
       --transition-id "operator-rollback-<变更单号>" \
       --service-uid "$(id -u macstudio0)" \
       --business-date "$(date +%F)"
   ```

5. 只在 `N+1` 完整 chain 校验通过后启动 legacy 服务。旧进程仍绑定 N，看到
   epoch 漂移必须拒绝；旧 occurrence 冻结 N/digest，后续更高 epoch 也不得恢复它。

从 legacy 再切回 ledger 同样必须全停、quiescence 全零、已安装 backend、
scheduler、v2-preflight 三份 plist mode 全为 `ledger`，且 v2-preflight 服务保持
bootout，然后才可追加更高 ledger epoch。
禁止删除 epoch directory、删改历史 record、重放同/低 epoch、直接覆盖最终
record 或靠仓库 rollout 降级。任何时刻都不得让两个模式并行写库。

威胁模型以 root/operator 为信任根：chain 防普通服务账号写入、误配置、旧进程和
正常运维 replay，不声称抵御恶意 root 删除整条 chain。运行中的进程能以
process-bound epoch 检测此漂移并 fail-closed；若恶意 root 删除整个目录后再完整
重启，系统无法在不增加 DB/硬件高水位锚的前提下区分“从未进入 epoch”，该情形
必须按未授权灾难恢复处理，不属于支持的回滚。

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
