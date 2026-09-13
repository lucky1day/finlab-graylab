# 双机部署与访问入口

**文档状态**：`CURRENT`

**连接信息核验日期**：2026-09-13

本文是主机地址、连接方法与部署路径的唯一运维入口。执行 ECS/Mac3 操作前先读本文，
再核对[当前状态](../CURRENT_STATUS.md)和现场 current、进程、控制面；无需从旧任务或历史发布记录重新查地址。
地址或路径发生授权变更时更新本文，release、数量、输入水位只更新当前状态。

## 三个角色与两条访问链路

| 角色 | 地址与用途 |
|---|---|
| ECS 独立灰度实验室 | `47.103.45.193`；运行独立 Backend、MySQL、DataBridge、systemd 调度。SSH：`root@47.103.45.193` |
| Mac3 生产 | 当前 Mac 主机、用户 `macstudio0`；运行生产 Backend、MySQL、DataBridge、launchd。公网入口：`https://bond.finailab.cn/bond-factor-lab/` |
| Mac3 公网中继 ECS | `101.132.143.185`；现有 Nginx/SSH 反向转发入口，**不是独立灰度实验室**。地址以 Mac3 installed tunnel plist 读回为准 |

```text
灰度访问：本机浏览器 http://localhost:18110/
          → 临时 ssh -L → 47.103.45.193:127.0.0.1:8100 → 灰度 MySQL

生产访问：https://bond.finailab.cn/bond-factor-lab/
          → 公网中继 ECS Nginx → 中继 127.0.0.1:18100
          → Mac3 已安装的 ssh -R → Mac3 127.0.0.1:8100 → 生产 MySQL
```

两端不共享数据库、输入或 DataBridge。公网域名访问到的是 Mac3；不能拿它验收灰度 ECS。
灰度临时转发与 Mac3 已安装的反向隧道分别拥有连接，不能互相替代或通过重建生产隧道排查灰度入口。

## 部署路径速查

| 项目 | ECS 独立灰度 | Mac3 生产 |
|---|---|---|
| `BFL_DEPLOYMENT_TARGET` | `aliyun-gray` | `mac3-production` |
| 控制面 | `systemd_one_shot` | `launchd_one_shot` |
| 部署根 | `/opt/bond-factor-lab` | `/Users/macstudio0/bond-factor-lab-production` |
| 运行代码 | 部署根下 `current -> releases/<commit>` | 同左 |
| 回滚候选 | 部署根下 `previous`，须另外核验数据/状态兼容性 | 同左 |
| `BFL_RUNTIME_ROOT` | `/var/lib/bond-factor-lab/state` | `/Users/macstudio0/bond-factor-lab-runtime` |
| 服务 Python | `/opt/miniconda3/envs/bond_factor_lab_service/bin/python` | `/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python` |
| 私有服务配置 | `/etc/bond-factor-lab/bond-factor-lab.env` | `/Users/macstudio0/bond-factor-lab-runtime/config/service.env` |
| 认证离线 secret | `/etc/bond-factor-lab/secrets/auth-bootstrap-password` | runtime 下 `config/auth-bootstrap-password` |
| release 环境 | 精确 release 内 `.bfl-release.env`，由同版本安装器生成 | 同左 |
| 安装控制面 | `/etc/systemd/system/bond-factor-lab-*` | `/Users/macstudio0/Library/LaunchAgents/com.bond-factor-lab.*.plist`，domain `gui/501` |
| 日志 | `journalctl -u <unit>`；外置文件见 runtime 下 `logs/` | runtime 下 `logs/com.bond-factor-lab.*.log` / `.err` |
| 收包与审计 | `/opt/bond-factor-lab/incoming/<delivery>` | runtime 下 `releases/<delivery>` |

开发工作区为 `/Users/macstudio0/bond-factor-lab`，不能作为生产 cwd；分支规则见[根规范](../../AGENTS.md#开发与操作权限)。
两机业务库名均为 `bond_db`，但实际实例、身份和连接必须从各自私有服务配置与只读查询核对；
不在文档保存 DSN、server UUID、密码、会话或 token。

Blackbox 解释器由 [Runtime Profile](../../deploy/blackbox_v2/runtime_profile_v1.json) 指定；依赖分别核对
[ECS frozen manifest](../../deploy/blackbox_v2/environment_manifest.json)和[Mac3 frozen manifest](../../deploy/blackbox_v2/environment_manifest.osx-arm64.json)。
Mac3 W4 仍使用 `/Users/macstudio0/miniconda3/envs/forecast_env/bin/python`，保留原 Native 依赖。
执行 release 内 Python 的不可变性规则见[部署手册](../../deploy/README.md#构建预安装和晋级)。

## ECS SSH 与临时本地转发

在当前 Mac 上，已核验的 SSH 身份文件位置是 `/Users/macstudio0/.ssh/finlab-key.pem`。
只记录路径，不复制密钥内容。SSH 均保留 strict host-key 校验；指纹通过独立可信渠道核验，
不能关闭校验或信任未经核验的 ssh-keyscan。失败时先查 known_hosts 与可信指纹。

```bash
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes \
  -i /Users/macstudio0/.ssh/finlab-key.pem root@47.103.45.193
```

先在 Mac 查看 18110 是否已有 listener；若已有，核对 PID 的 SSH 目标，复用正确连接，不杀未知进程：

```bash
lsof -nP -iTCP:18110 -sTCP:LISTEN
```

需要浏览器访问独立灰度 ECS 且端口空闲时，在一个独立终端运行以下前台转发。它不安装 service、
不修改 SSH 配置或生产反向隧道；关闭该终端或 Ctrl-C 只结束这一条临时连接。

```bash
ssh -N -T \
  -L 127.0.0.1:18110:127.0.0.1:8100 \
  -o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes \
  -o ExitOnForwardFailure=yes -o ConnectTimeout=10 \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -i /Users/macstudio0/.ssh/finlab-key.pem root@47.103.45.193
```

打开 **`http://localhost:18110/`**。ECS 页面没有 `/bond-factor-lab/` 前缀；不要改为
`http://127.0.0.1:18110` 登录，因为认证 trusted Origin 精确为 `http://localhost:18110`。

```bash
curl --fail --silent --show-error http://localhost:18110/api/health
```

Mac3 则使用 **`https://bond.finailab.cn/bond-factor-lab/`**，其 trusted Origin 是
`https://bond.finailab.cn`。两机登录独立；使用现有授权账号正常登录，不在 README、脚本、参数或日志中保存密码。
普通浏览器可在 localhost 可信上下文使用 Secure 会话 cookie；命令行 HTTP 客户端行为可能不同。
HTTP 401 先核对入口和会话，不修改认证配置，也不通过重置管理员排查网络。

## 每次操作前的只读读回

在灰度 ECS 的 SSH 会话中执行：

```bash
readlink /opt/bond-factor-lab/current
readlink /opt/bond-factor-lab/previous
systemctl show bond-factor-lab-backend.service -p MainPID -p ActiveState -p WorkingDirectory
systemctl list-timers --all --no-pager 'bond-factor-lab-*'
curl --fail --silent --show-error http://127.0.0.1:8100/api/health
```

用读回的 Backend PID 检查 `/proc/<PID>/cwd`，须指向 current 解析后的精确 release；
`WorkingDirectory=/opt/bond-factor-lab/current` 本身不能证明进程已经更新。
预测单元分别为 `bond-factor-lab-prediction-daily`、`-weekly`、`-monthly` 的 `.service/.timer`；
另有 `bond-factor-lab-data-bridge`、`bond-factor-lab-actuals` 与 `bond-factor-lab-backend.service`。

在 Mac3 执行：

```bash
readlink /Users/macstudio0/bond-factor-lab-production/current
readlink /Users/macstudio0/bond-factor-lab-production/previous
launchctl print gui/501/com.bond-factor-lab.backend
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -B \
  /Users/macstudio0/bond-factor-lab-production/current/scripts/audit_launchd_config_drift.py \
  --project-root /Users/macstudio0/bond-factor-lab-production/current \
  --runtime-root /Users/macstudio0/bond-factor-lab-runtime
curl --fail --silent --show-error http://127.0.0.1:8100/api/health
```

用读回的 Backend PID 执行 `lsof -a -p <PID> -d cwd -Fn` 核对实际 cwd。
Mac3 label 为 `com.bond-factor-lab.backend`、`daily-predictions`、`weekly-predictions`、
`monthly-predictions`、`data-bridge-refresh`、`actuals`、`ssh-tunnel`（后六项也带相同前缀）。
正常日历、发布/回滚和权限边界见[部署运行手册](../../deploy/README.md)；实际在途 run、输入 ready、
Registry/exact 与下一触发时间还须按[入库导航](../onboarding/README.md)及[调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)核对。

## 前端验收常见问题

- `localhost:18110` 连接失败：先查本机转发，再查灰度 ECS 8100；不是 Mac3 公网入口故障。
- Mac3 公网失败：按[三点链路探针](PUBLIC_FACTOR_LAB_PERFORMANCE.md#故障定位与恢复)核对 Mac3 8100、中继 18100、公网。
- Dashboard 的认证响应、明细参数及验收限制见[Dashboard 合同](PUBLIC_FACTOR_LAB_PERFORMANCE.md#认证响应与合同验收)。

以上连接说明不授予生产变更权限；操作边界见根规范与所选流程。
