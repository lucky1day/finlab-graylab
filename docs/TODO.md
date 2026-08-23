# 统一后续推进计划

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-23

本文只列当前尚待推进或尚需独立生产授权的工作。当前事实见[当前状态](CURRENT_STATUS.md)，生产规则见[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成工作通过 Git 和控制面审计追溯，不在这里保存关闭清单。

## 当前队列

本页只保留尚未闭环的观察、诊断与未来生产项目：

1. 月均、季均、年均三种 Blackbox V2 任务的基础建设尚待实施。本轮只建立统一任务规格、MID/CQ/SF
   桶语义、通用周期 actual、单一收盘后 one-shot、现有 Dashboard API 和九列前端能力；不得提前 Intake
   参考的 20 个 M0 方案，不应用 migration，不修改 ECS/Mac3 installed 调度，也不把平台 actual 与算法信号
   混为一套计算。实施必须保持 `target_date` 直接计算，不增加业务桶 ID、季度/年度 cutoff key、三张 actual
   表或三个调度器。
2. `five_y_factor_rule_online_v1` 与 `ten_y_factor_level_ensemble_v1` 已在 ECS、Mac3 分别完成技术 Gate、
   shadow、canonical backtest、activation、`gray_live` 和 DashboardGate，两端当前均为 Onboarding
   Complete。等待 2026-08-24 07:03 Asia/Shanghai 的首次真实 daily systemd/launchd 自然触发；触发后
   须按主机分别核验 service/job 日志、`scheduled_live` run/prediction 与 Dashboard。不得为提前取得观察
   证据而 kickstart、覆盖日期或倒签信号。M0 周平均五方案及同期 ECS 周频方案另等待
   2026-08-29 11:30 的首次自然触发并按同样证据闭环。
3. 独立诊断 Mac3 2026-08-21 daily 的 17 个 `execution_failed`。该批次已结束且无残留 runner；本次方案
   晋级未重跑、未补写、未修改其历史 run。诊断须先按失败原因分组并读取既有 run/log evidence，
   不得把前端发布成功外推为 daily 已恢复，也不得在没有新的业务写入授权时手工重跑。
4. 若未来决定把生产域名或 Writer 从 Mac3 切到 ECS，必须作为新的生产项目重新设计 Web、数据库
   authority、单 Writer、DNS/Nginx、切换窗口和回滚范围，不能从当前灰度或 immutable release 验收外推授权。
5. 独立评估 systemd/launchd runner 的 CLI 日期入口；设计并取得生产授权后替换 ECS installed
   DataBridge、daily、weekly、monthly 四个 unit，执行 `systemctl daemon-reload` 并读回现场状态。
   执行前只读确认 `/run/bond-factor-lab/manual-run.env` 不存在且相关 one-shot 任务 idle，不得从仓库
   模板变更外推现场状态或授权。

ECS 继续独立灰度运行。Mac3 域名、Nginx、DNS、数据库 authority 和生产 Writer 均保持不变；是否
未来切到 ECS 是新的生产项目，不是本轮双主机代码治理闭环的前提。

## 评审边界

- 后续 release 更新仍须避开正在运行的生产批次，并分别授权 installed plist、loaded state、服务和
  数据库操作。
- 不因文件较大就拆分，不因极小概率事件增加 fallback、兼容层、重试、第二控制面或额外 hash。
- 只有能证明业务职责已经重复、没有调用者、被现行规则替代或妨碍错误直接暴露的设计，才进入删除候选。
- 任一实施建议都必须独立获得用户确认，并以可复现现象和聚焦回归证明删除没有放宽 Gate、授权、insert-only、生命周期或输入截止约束。

## 统一停止条件

- 需要改变算法逻辑、生产调度、数据库 schema、installed plist、服务状态或业务数据；
- 需要用 fallback、旧版本双读或隐式兼容才能让测试通过；
- 发现当前文档、代码、数据库或现场状态与评审前提不一致。
