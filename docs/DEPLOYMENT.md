# 部署说明

**目标机器**: 当前测试 Mac

**服务端口**: `127.0.0.1:8100`

**数据库**: `localhost:3306/bond_db`

**更新日期**: 2026-06-05

## 1. 配置 `.env`

新机器当前已经存在 `.env`，权限为 `600`，并由 `.gitignore` 排除。首次部署或重新生成时，从 `.env.example` 复制本机配置，并补充数据库密码:

```bash
cp .env.example .env
```

服务层和调度器都会通过 `shared.db_config` 自动读取 `.env`。不要把 `.env` 提交到 Git。

## 2. 手动验证

只读/不写库验证:

```bash
curl -sS http://127.0.0.1:8100/api/health
curl -sS http://127.0.0.1:8100/api/targets
conda run -n forecast_env python -m scheduler.scheme_runner --scheme-id t1_daily --predict-date 2026-06-03
conda run -n forecast_env python -m scheduler.scheme_runner --scheme-id t5_daily --predict-date 2026-06-03
```

需要明确刷新正式表时再执行写库命令:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -m scheduler.daily_actuals_updater --end-date 2026-06-03
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -m scheduler.executor 2026-06-03
```

如 launchd 未运行，可临时手动启动后端:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service uvicorn backend.main:app --host 127.0.0.1 --port 8100
```

打开:

```text
http://127.0.0.1:8100/
```

## 3. launchd 安装

plist 模板位于 `deploy/launchd/`:

- `com.bond-factor-lab.backend.plist`
- `com.bond-factor-lab.scheduler.plist`

安装到当前用户 LaunchAgents:

```bash
mkdir -p ~/Library/LaunchAgents
cp deploy/launchd/com.bond-factor-lab.backend.plist ~/Library/LaunchAgents/
cp deploy/launchd/com.bond-factor-lab.scheduler.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.bond-factor-lab.backend.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.bond-factor-lab.scheduler.plist
```

重启服务:

```bash
launchctl kickstart -k "gui/$(id -u)/com.bond-factor-lab.backend"
launchctl kickstart -k "gui/$(id -u)/com.bond-factor-lab.scheduler"
```

卸载服务:

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.bond-factor-lab.backend.plist
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.bond-factor-lab.scheduler.plist
```

日志默认写入:

- `/tmp/bond-factor-lab-backend.log`
- `/tmp/bond-factor-lab-backend.err`
- `/tmp/bond-factor-lab-scheduler.log`
- `/tmp/bond-factor-lab-scheduler.err`

## 4. 当前安装状态

2026-06-06 已在当前用户 LaunchAgents 下确认 backend 和 scheduler 均已启动。

验证命令:

```bash
launchctl list | rg 'bond-factor-lab|PID|Status'
curl -sS http://127.0.0.1:8100/api/health
tail -n 80 /tmp/bond-factor-lab-scheduler.err
```

当前新机数据已补齐到本轮测试口径，`t1_daily`、`t5_daily` 和 `weekly_10y_d_overlay` 均为 `active`。2026-06-05 09:25 scheduler 已写入 t1/t5 共 6 条正式预测和 2 条 success run_log；2026-06-06 周度 10Y 已完成受控 live 写库验收和单方案 scheduler 手动补跑，正式预测表中 `weekly_10y_d_overlay` 通过 UPSERT 保持 1 条记录，run_log 累计 2 条 weekly success。10Y 周度 actuals 已写入独立 `t_scheme_weekly_actuals` 表 794 条；2026-06-06 15:05 已重启 scheduler 并确认注册 `Scheduled scheme weekly_10y_d_overlay at 30 11 * * 6`。周度 live cron 为周六 `11:30`，对齐旧实盘 weekly `multi` 任务的首轮预测时间；旧脚本 16:00 / 22:00 为检查和必要补跑节点。周度 scheduler job 已设置 `force=True`，避免周六预测被通用非交易日判断跳过。后续重点是观察下一次周六 11:30 自动预测和周度 target actual 落库。拿到外层仓库路径后再完成 panda_quantflow iframe 接入。
