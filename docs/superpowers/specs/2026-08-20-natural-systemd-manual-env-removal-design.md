# ECS 自然 systemd 模板手工环境移除设计

**状态**：待实施

**日期**：2026-08-20

## 目标

从仓库期望配置中移除 ECS 自然 DataBridge、daily、weekly、monthly one-shot 对
`/run/bond-factor-lab/manual-run.env` 的可选加载，防止一次性手工环境文件被后续 timer 反复继承。

## 根因

四个自然 service 模板都声明：

```text
EnvironmentFile=-/run/bond-factor-lab/manual-run.env
```

其中 prediction runner 还会读取 `BFL_SYSTEMD_PREDICT_DATE`。现场该文件当前不存在，所以没有正在
生效的旧日期；但模板仍允许未来遗留文件持续影响自然调度。

## 第一阶段范围

- 从以下四个仓库模板删除上述单行：
  - `deploy/systemd/bond-factor-lab-data-bridge.service`
  - `deploy/systemd/bond-factor-lab-prediction-daily.service`
  - `deploy/systemd/bond-factor-lab-prediction-weekly.service`
  - `deploy/systemd/bond-factor-lab-prediction-monthly.service`
- 修改 `tests/test_systemd_control_plane.py`，要求所有仓库自然 systemd service 都不得引用
  `manual-run.env`。
- 更新 `deploy/README.md`、`docs/CURRENT_STATUS.md`、`docs/TODO.md` 和
  `docs/operations/ALIYUN_MIGRATION_ASSESSMENT.md`，明确第一阶段只关闭仓库模板的持久手工环境入口。

## 非目标

- 不修改 `scheduler.systemd_prediction_runner` 或 `scheduler.launchd_prediction_runner`。
- 不删除 CLI `--predict-date`，不改变内部 `run(..., predict_date=...)`。
- 不替换 ECS installed unit，不执行 `systemctl daemon-reload`、启动、停止或手工触发任务。
- 不修改 Mac3 plist、服务、数据库、算法、Registry 或缓存。

## 验收

- 新合同测试在旧模板上因四个 `manual-run.env` 引用失败，删除后通过。
- 四个模板不再包含 `/run/bond-factor-lab/manual-run.env`，其余 EnvironmentFile 顺序、命令、时限和
  timer 语义不变。
- systemd 聚焦测试、文档门禁和全量测试通过。
- ECS 现场只读状态保持原样；installed unit 仍有旧引用的事实记录为下一阶段待办，不误称已经生效。

## 后续阶段

第一阶段通过后，再独立评估 runner 日期入口和 ECS installed unit 替换。后续操作需要新的设计与
生产授权，不能从本阶段外推。
