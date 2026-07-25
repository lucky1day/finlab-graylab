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

## 当前运维基线

2026-07-24 日频 SLA 架构复审后的本地运行基线：

- backend 和 scheduler 均由 launchd 管理，服务端口仍为 `127.0.0.1:8100`。
- scheduler 调整配置、方案 `config.yaml` 或代码后，必须先确认当前
  `BOND_DAILY_COORDINATOR_MODE` 和 rollout 门禁；只有在获准的维护窗口内才能
  kickstart。不得通过 07:00 自动重启修复日频调度。
- scheduler 的 launchd 配置使用 `RunAtLoad=true` 与 `KeepAlive=true`：Mac 登录该用户会自动拉起，进程退出会被 launchd 重新拉起。若需要无人登录前启动，应另行制作 root `LaunchDaemon`，不能直接复用当前依赖用户 conda 环境的 `LaunchAgent`。
- ledger 候选路径由一个 06:30 coordinator 冻结日批账本并检查 T-1 最小
  readiness；未齐备时不创建算法 attempt，由 recovery tick 重试。就绪后立即
  创建 Native RR generation 并启动当天 DataBridge generation；Native
  snapshot 可在 08:30 recovery cutoff 前开启，且会在同一快照内复核 1Y/3Y/
  5Y/7Y/10Y 五个曲线锚点；06:30 只是 hard not-before。
  06:55 是 DataBridge readiness 审计点：未就绪即
  `LATE`/告警，但 ledger 仍继续刷新当天新 generation 到 08:30；07:00 只做
  watchdog，不重启 scheduler。
- 17 个 Native 中 14 个固定使用 `generation_v1`；仅
  `daily_1y_xgb_1y13_0629`、`daily_5y_lgbm_5y10_0629` 和
  `daily_10y_lgbm_10y04_0629` 可使用 `live_source_0629` MVP 兼容桥。三者仍
  绑定当天 Native generation fence，由 ledger `started_at` 记录启动；policy
  冻结 source package hash，子进程复制后重验，prediction extra 记录并复核
  source 输出水位。其它 Native 不得使用，generation 任务失败不得回退。平台
  pre-run artifact 仅证明 readiness 观察，不冒充算法输入。该桥未取得切换资格。
- 仓库中的 scheduler、backend 和 V2 preflight 三份 launchd 配置，以及
  `deploy/daily_coordinator_rollout_v1.json`，均显式保持
  `BOND_DAILY_COORDINATOR_MODE=legacy`/`mode=legacy`，表示生产切换尚未授权。
  rollout 文件只兼容尚未补环境变量的旧 launchd 安装，不是 ledger 切换开关；
  切换后必须以已安装 plist 的显式环境为准。legacy 模式下 preflight 的四个
  calendar trigger 仍是每日 DataBridge refresh 的唯一 owner；提前禁用会让
  跨日常驻 scheduler 在下一日没有新 DataBridge 数据。
- ledger 切换时先 bootout legacy preflight、scheduler 和 backend，确认旧
  refresh/writer 全部退出；再把三份**已安装** plist 一起改成 `ledger`。服务
  仍停止时向 machine-global、root-owned append-only epoch chain 原子发布
  genesis epoch，最后才按 backend、单一 scheduler 的顺序启动。受控回滚同样
  必须全停服务、把三份已安装 plist 一起改为 `legacy`，并追加更高 epoch；
  不得删除 chain 或复用旧 epoch。两种路径不得并行；仓库中的三份模板和 rollout
  文件始终保持 `legacy`。
- ledger 启动补偿只通过同一 occurrence coordinator：复用冻结 Registry 和
  generation，跳过成功 item，08:30 后不再启动 attempt。legacy 模式仍保留旧
  per-scheme catch-up，作为切换前的受控兼容路径。
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

### 4) 本地 Mac：日频 coordinator rollout

`deploy/launchd/com.bond-factor-lab.scheduler.plist` 当前默认设置：

- `BOND_DAILY_COORDINATOR_MODE=legacy`：切换门禁未通过，继续使用旧路径。
- `BFL_SOURCE_DB_CONFIG_ROOT=/Users/macstudio0/.config/bond-factor-lab` 与
  `BFL_SOURCE_DB_CONFIG_PATH=/Users/macstudio0/.config/bond-factor-lab/source-runtime-db.json`：
  前者固定批准的 BFL 私有配置根，后者只注入 source-readonly 配置文件的位置；
  不得把用户名、密码或 DSN 写入 plist。
  安装或重启 scheduler 前必须确认私有目录和终端文件权限：

  ```bash
  chmod 700 /Users/macstudio0/.config/bond-factor-lab
  chmod 600 /Users/macstudio0/.config/bond-factor-lab/source-runtime-db.json
  ```

  配置缺失或权限、owner、inode、symlink 检查失败时，三个 0629 source-backed
  任务必须在算法启动前 fail-closed。scheduler 启动时先执行
  `SHOW GRANTS FOR CURRENT_USER()` 的保守授权审计，再对由统一数据契约声明的
  9 张必需源表逐表执行零行 SELECT；只有直接 `USAGE/SELECT`、无 role、无
  grant option、无未知权限时才允许继续 Registry 同步和任务注册。
  生产预检禁止执行 DDL/DML；INSERT/UPDATE/DELETE/CREATE/ALTER/DROP 的拒绝
  证据只在隔离临时 MySQL 中采集。仓库模板的更新不授权覆盖已安装 plist，
  也不授权 kickstart；必须留到 BFL 专项维护窗口逐项读回验证。该配置仅属于
  Bond Factor Lab，不得修改或重启 BondProjectPro。
- `deploy/daily_coordinator_rollout_v1.json` 同样固定 `mode=legacy`，仅供旧
  launchd 缺少环境变量时安全 bootstrap；machine-global epoch directory 当前
  不存在，ledger 上线不能只改这个文件或任一份 plist。epoch directory 一旦
  存在，rollout 文件不再参与裁决。
- `DATABRIDGE_REFRESH_START=06:30`、`DATABRIDGE_REFRESH_DEADLINE=06:55`：
  legacy preflight 仍使用该 deadline；ledger coordinator 以版本化 policy 的
  06:55 readiness guardrail 做审计，并显式把刷新 hard deadline 设为 08:30，
  不因 06:55 告警而回退旧 `current` 或停止当天刷新。
- `BOND_SCHEDULER_STAGGER_MINUTES=2` 和
  `BOND_SCHEDULER_PREDICTION_MAX_CONCURRENCY=1` 只控制 legacy 回滚路径；
  ledger 不用 APScheduler 线程 + job 内 semaphore 排队。
- `BOND_SCHEDULER_STARTUP_CATCHUP=1`：legacy 时调用旧 catch-up；ledger 时
  只调用 occurrence recovery。

ledger 切换前必须按
[日频信号 08:00 SLA 架构](../docs/architecture/DAILY_SIGNAL_SLA.md)
完成迁移、0629 generation 输入替换、20 次 forced-cold、20 次真实 revision、
故障注入和连续 10 个交易日 25/25。以下只读 CLI 仅做离线统计/结构评估，
输出中的 `runtime_admission_eligible` 固定为 `false`；它不能直接打开 rollout：

切换维护窗口内还必须在服务停止时核验并准备 7 个日频存储根：
`~/Library/Application Support/BondFactorLab/daily-runtime-v1`、
其下的 `occurrence-locks`、
`backtest_artifacts/input_generations/native`、
`backtest_artifacts/input_generations/databridge`、`data/data_bridge` 和
`backtest_artifacts/data_bridge_refresh`，以及
`backtest_artifacts/runtime_cache/liwei_0616`。逐个确认路径及父链无
symlink、owner 为实际 scheduler 服务 UID，再显式创建/调整为 `0700`；迁移后
以同一服务 UID 完成 create/open/cleanup/preflight 演练。scheduler 启动会先
统一检查全部根，ledger occurrence 与 operator recovery 在任何 attempt 前再次
检查。runtime 只会以 `0700` 创建不存在的目录；对既有 `0755`、错误 owner 或
symlink 一律 fail-closed，不会静默 chmod，也不会在发现任一不安全根后创建
其它缺失根。

容量联跑前必须先用真实、交易日合法 capture window 产生的 `SEALED` Native
generation 认证 14 个 `generation_v1` 日频方案。认证只允许在与候选 commit
一致的 clean detached worktree 中运行，`forecast_env` 是唯一算法环境，输出根
必须位于 worktree 外且预先以 `0700` 创建。runner 固定串行执行、禁止网络和
业务持久化；每个 worker 使用 macOS sandbox 将候选 worktree 和 generation
挂为只读、只允许写外部证据根，并通过最小环境 allowlist 清除数据库连接信息。
原算法的 `multiprocessing.Pool` 必须保持 `forecast_env` 的 spawn 模式；policy
只允许 fork、POSIX semaphore 和精确 Python executable 的 exec，所有 spawn
子进程继续继承同一断网和只读边界，不允许其它 executable 或越界写入。
父进程在每次执行前后重新核对 detached HEAD、clean 状态、tracked policy SHA
和输出根 inode，worker 也独立核对同一 policy；任一漂移或孤儿进程均
fail-closed。t1/t5、V28 各独立双跑，10 个 Liwei 按 cache family 执行
cold / warm-build / warm-hit，并比较除明确路径/cache audit 外的全部 canonical
字段，同时核对共享 family 的 cache generation lineage。cache、日志和
`certification.json` 留在忽略的外部证据目录，原 SEALED generation 保持只读；
二者都不得提交 Git，也不能据此自动打开 admission。

```bash
cert_root="$(mktemp -d /tmp/bfl-native-cert.XXXXXX)"
chmod 700 "$cert_root"
cert_root="$(cd "$cert_root" && pwd -P)"
cd <clean-detached-candidate-worktree>
conda run --no-capture-output -n forecast_env \
  python -B scripts/certify_generation_native_daily.py \
    --manifest <absolute-sealed-native-manifest> \
    --output-root "$cert_root" \
    --no-persist
```

不得通过伪造历史 snapshot clock、使用周末 generation 或复用旧 live DB 报告来
替代交易日日批认证；当前没有合法 generation 时只允许提交和验证 runner 本身。

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/evaluate_daily_capacity_gate.py <attested-evidence.json> \
    --expected-machine-id <approved-mac-id> \
    --expected-policy-version daily-scheduler-policy-v1 \
    --expected-policy-sha256 <policy-file-sha256>
```

生产 `ADMITTED` 还必须同时具备：

- collector 对完整 evidence 的 detached CMS 签名；
- 独立 operator 对带 decision id/sequence/有效期的 canonical decision 签名；
- 两套不同的 root-owned 专用 Keychain 和 trust 配置，逐证书固定 DER
  SHA-256，并禁止同一签名者承担两种角色；
- admission、evidence、签名和 trust 文件的 owner/mode/父目录均通过
  no-follow 安全读取；
- 签名 candidate 精确覆盖当前 21 item/25 target、Registry/version、Mac
  identity、三套 conda 环境的 explicit manifest 与 canonical 全包清单
  （含 `channel=pypi`）、scheduler/scheme 代码、runtime profile、migrations、
  两类 exporter、MySQL ledger schema definition，以及 candidate v2 的
  control-plane identity：service UID、解析后的 machine-global runtime root、
  epoch-chain directory 路径、固定 contract/genesis identity。active epoch
  不进入 capacity candidate，避免每次受控切换重做 20+20；它改由 occurrence
  policy 和 heartbeat 冻结。ledger Native
  环境固定为 `forecast_env`，CLI/admin 传入其它 `algo_env` 必须拒绝。

scheduler 启动、06:30/recovery coordinator 和 operator recovery 会重新采集
当前 candidate，并与签名 candidate 做 canonical fingerprint 精确比较；该
fingerprint 同 policy 一起冻结进 occurrence，恢复和 watchdog 必须保持一致。
30 秒 heartbeat 只复核有时效的签名 admission，不重复执行昂贵的全量 rehash。
首次 occurrence 写入后、任何 generation/reconcile/attempt 前还会重算一次当前
candidate，封住准入检查与 Registry/version 冻结之间的漂移窗口。
仓库的 `deploy/daily_capacity_admission_v2.json` 默认是 `BLOCKED`；不得通过
手工把 `status` 改成 `ADMITTED` 代替上述签名链和证据。

切换必须在获准维护窗口一次完成。以下“已安装 plist”均指
`/Users/macstudio0/Library/LaunchAgents/` 下的实际 launchd 输入。仓库当前
rollout、三份 plist 和 admission 仍分别保持 `legacy`/`BLOCKED`；以下步骤是未来
获得专项授权后的人工 SOP；应用启动不会自行创建或追加 epoch：

1. 阻断 admin/operator recovery，依次 bootout legacy V2 preflight、scheduler
   和 backend。等待或终止经核验的 per-scheme scheduled-live、refresh 和 API
   background 子进程；不能只 `kickstart -k`。在 machine-global epoch 发布前，
   三者都不得重新启动。
2. 确认 writer/refresh 均停止后制作一致性备份；只能通过
   `conda run -n bond_factor_lab_service python scripts/apply_migrations.py --apply`
   应用 pending migration，并保存 history、preflight 与 postcondition 报告。
   `--apply` 是不可省略的写库授权；help、空参数或未知参数必须在创建数据库
   engine 前退出，不能把参数错误静默解释成执行迁移。
   runner 会对无 history 的 v16 生产库先做完整 baseline 再登记 001..016，绝不
   重放历史数据迁移。禁止用 `mysql` 或其它客户端直跑 017 SQL：SQL guard 只是
   纵深防御，不能替代版本、`sql_mode`、UTC/FK、legacy 数据和闭世界 definition
   fingerprint，也不能把 implicit-commit DDL 误当成可回滚事务。

   若 runner 因断连或进程退出留下 `017=APPLYING`，普通 `--apply` 必须继续
   fail-closed。保持全部 writer 停止，先执行只读检查并保存完整 JSON：

   ```bash
   conda run --no-capture-output -n bond_factor_lab_service \
     python scripts/apply_migrations.py --inspect-applying-017
   ```

   检查只会返回 `COMPLETE`、`COMPATIBLE_PARTIAL` 或 `UNSAFE`，并把 migration
   filename/checksum、连续 001..017 history、MySQL server UUID、database name、
   schema fingerprint 和数据反例探针绑定进 `state_digest`。`UNSAFE` 不存在自动
   恢复路径，必须先人工定位 drift；不得通过改 history、删对象或伪造 digest
   绕过。前两种状态经双人核验后，使用**同一次检查原样输出**的 digest 显式授权：

   ```bash
   conda run --no-capture-output -n bond_factor_lab_service \
     python scripts/apply_migrations.py \
       --recover-applying-017 --apply \
       --state-digest <inspect-json中的64位state_digest>
   ```

   recovery 会在同一 migration owner lock 内重读状态；任何 digest 漂移都拒绝。
   `COMPLETE` 只在单个事务中把精确的 017 history row 标为 `APPLIED`，不重放 SQL；
   `COMPATIBLE_PARTIAL` 只接受 migration 017 可安全重放的缺失对象，以及
   `feature_date/resource_class/internal_workers/release_offset_minutes` 四个精确
   nullable 过渡定义，随后幂等重放 017 并通过完整 postcondition 后才标记。
   replay、postcondition 或 mark 任一步失败都保留 `APPLYING`，必须重新 inspect，
   不能复用旧 digest。
3. 仓库 `deploy/daily_coordinator_rollout_v1.json` **继续保持
   `legacy`**；它只服务“epoch directory 完全不存在”的初始 bootstrap，不能作为生产 cutover
   开关。只把**已安装的** scheduler、backend、V2 preflight 三份 plist 的
   `BOND_DAILY_COORDINATOR_MODE` 同时改为 `ledger`，逐份用 `plutil`/
   `PlistBuddy` 读回；此时服务仍全部停止。重新生成 current capacity candidate，
   必须与已签名 candidate v2 精确一致；任何 service UID、runtime root、epoch contract
   identity、commit 或代码漂移都中止切换。不得靠 production dirty worktree
   修改受版本控制 rollout JSON。
4. 双人核验后，使用 root operator 发布固定 genesis。父目录及其受管父链必须
   root-owned 且不可被 group/other 写。epoch root/`records` 为 `0755`，
   record 为 `0644`，使 `macstudio0` LaunchAgent 可读但所有非 root 不可写；
   `staging` 为 `0700`，并与用户可写的 occurrence/runtime root 完全分离。
   operator 先在同盘 `staging` 以 `O_EXCL` 创建临时文件，完整写入、fsync、
   chmod、逐字节复核，再以 hard-link no-clobber 原子发布并 fsync `records`
   目录。禁止直接向最终 `epoch-N` 边写边发布，禁止重定向、`mv -f` 或覆盖：

   ```bash
   sudo install -d -o root -g wheel -m 0755 \
     "/Library/Application Support/BondFactorLab"
   sudo --preserve-env=BOND_DB_USER,BOND_DB_PASSWORD,BOND_DB_HOST,BOND_DB_PORT,BOND_DB_NAME \
     /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
     scripts/daily_coordinator_epoch_operator.py \
       --expected-current-epoch 0 \
       --mode ledger \
       --transition-id bond-factor-lab-daily-ledger-genesis-v1 \
       --service-uid "$(id -u macstudio0)" \
       --business-date "$(date +%F)"
   ```

   工具会确认三份 LaunchAgent 已 bootout、三份已安装 plist mode 全为目标 mode，
   进程表中没有遗留 scheduler/backend/preflight/算法进程，DB 中全局（包括历史
   和未来业务日期）没有非终态 occurrence/item、running run、待清 orphan fence
   或旧路径 running scheduled-live。`--business-date` 仅作为本次变更审计字段，
   不会缩小静默检查范围。
   target mode 为 `ledger` 时还会在发布前重算 candidate 并验证当前签名 capacity
   admission；仓库默认 `BLOCKED` 会直接拒绝。随后执行连续 epoch、previous
   digest、canonical JSON、symlink、owner/mode 和 service UID 可读性检查。
   任一失败必须保持服务停止。`staging` 中的截断临时
   文件不进入 reader；最终 record 一旦出现，即使截断也必须 fail-closed 且
   不得覆盖。发布完成但调用方未收到返回时，以相同
   `expected-current-epoch/mode/transition-id` 重试只返回
   `already_published`。
5. 先 bootstrap backend，再只启动一个 scheduler；V2 preflight 保持 bootout，
   或验证其 ledger 配置启动后只返回 `disabled`。用 `launchctl print` 核验三份
   实际进程环境都是 `ledger`；同时验证
   `/api/health.daily_schedule.mode=ledger`、独立 heartbeat、occurrence、
   machine-global occurrence lock 和 DataBridge refresh owner 均唯一。

不能先启动 ledger 再停止 legacy，也不能让旧 preflight 重启新 scheduler。
维护窗口结束前还必须模拟一次 login/reload 检查：已安装 preflight 不得以
`legacy` 重新出现。若需受控回滚，必须另开全停维护窗口并按“回滚”章节追加
更高的 `legacy` epoch；单独改 plist、删除 epoch 或重放旧 epoch 都是配置故障。
仓库 rollout 继续保持初始 bootstrap 所需的 `legacy`。
2026-07-24 本机 `bond_db` 已应用 017 schema，但 launchd、scheduler/backend
mode 和服务进程均未切换，machine-global epoch chain 也未创建；上述其余切换步骤
仍未执行。

ledger 模式下，独立 DataBridge 命令只允许做只读检查，而且 mode 必须在命令中
显式给出；不带 mode 的命令不属于获准的生产操作：

```bash
BOND_DAILY_COORDINATOR_MODE=ledger \
  conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/refresh_data_bridge_current.py --check-only \
    --date "$(date +%F)"
```

`--publish`、`--dry-run` 和把
`python -m scheduler.main --run-once data-refresh` 当成刷新入口都不允许；后者
在 ledger 模式只能检查 current。实际刷新和发布只能由当日 occurrence
coordinator 持有。

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
occurrence 的动态 item/target 全集；当前 policy 基线是 21/25，Blackbox V2
不会被排除。08:00 时 24/25 必须返回 error，晚到补齐不改变原 SLA 结果。
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

从 legacy 再切回 ledger 同样必须全停、quiescence 全零、三份 plist 全为
`ledger`、当前 capacity admission 为 `ADMITTED`，然后追加更高 ledger epoch。
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
