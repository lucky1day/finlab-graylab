# 当前状态

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-20

本文只记录当前稳定事实。实时方案、run、prediction、DataBridge、API 和调度状态必须从各自权威数据源读取；除当前部署基线所需的精确 release 身份外，不在仓库文档冻结时点数量、运行 ID 或逐次校验 hash。未批准工作见[统一后续推进计划](TODO.md)，生产调度规则见[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 当前双主机运行规则

- Mac3 继续承载生产域名、前端、数据库和 Writer；`launchd + installed plist` 是 Mac3 的自然生产调度控制面。
- 双主机共用唯一 `codex/develop` source release 代码线，但允许按“ECS 先验证、Mac3 后晋级”分阶段
  发布；两端 `current` 不要求在灰度验证期始终相同，也不因此建立环境分支。
- Mac3 `current` 仍为 tag `mac3-immutable-r2-20260820` 对应的精确提交
  `e692285d47e41c384dc915758abe0c51f9ac3aaf`。ECS `current` 已晋级为 immutable prediction release
  `5c5603a23266e563e142e319d4e5d13907649598`，`previous` 为上述 R2 提交。
- 因此 insert-only prediction completion 当前只在 ECS 灰度运行态生效；Mac3 的 prediction Writer 仍运行旧
  `e692...` 语义。Mac3 晋级同一份 ECS 已验证 `5c...` archive 需要独立夜间生产窗口，在完成前不得
  把 ECS 验收外推为 Mac3 已具备该写入保护。
- Mac3 生产应用从独立 `bond-factor-lab-production/current` 启动；运行状态位于外置
  `bond-factor-lab-runtime`。`runtime/config/service.env` 是生产应用本机配置 authority，Git 根 `.env`
  只服务开发工作区；两者不自动同步，生产配置变更必须独立授权并重启对应服务。
- 同日 Mac3 已正式切换到精确 R2：六个应用 plist 均从 production `current` 启动，SSH tunnel 的
  工作目录和日志已外置；七项 drift audit、Backend/首页/方案/admin、DataBridge check-only 和远端
  loopback tunnel 读回全部通过。生产进程不再引用 Git 工作区，旧七个 plist 保留为首次回滚备份。
- Mac3 Git 开发根在保留既有未跟踪草稿的前提下切到 `codex/develop`；该分支切换不再影响生产进程。
- ECS 是独立灰度实验室，不是 Mac3 热备或复制节点；其 DataBridge、daily、weekly、monthly、Actuals 五个 systemd timer 已于 2026-08-18 经专项授权启用，现场 authority 是 installed unit/timer 与 `systemctl` 读回。
- ECS immutable prediction release 已完成不可变预安装、`current/previous` 原子切换和 Backend 重启；
  Backend 健康、首页、方案 API、五个 timer 与 installed unit/template 一致性均已读回。高频采样观察到的
  Backend 切换窗口约为 1.0036 秒，切换期间 ECS 灰度主库 `bond_db` 没有业务写入；Mac3 服务未受影响。
- ECS 一次性隔离 MySQL 真库直接调用了 Native active completion，并验证共享的 business-key decision
  与 plain INSERT：首次发布整组成功，完整重复 benign `skipped` 且旧行不变，部分冲突整组失败且缺失键
  不补写。Blackbox active completion 复用同一 repository decision/plain-INSERT 核心，本次未在隔离
  MySQL 中单独调用，其运行时入口由本地完整回归覆盖。
- 验证前后 ECS 灰度主库 `bond_db` 的 `t_scheme_predictions`、`t_scheme_versions`、
  `t_scheme_registry`、`t_scheme_runs`、`t_scheme_run_log` 五表 count 与全行摘要、五表 schema 摘要以及
  17 条 migration history 均一致；Mac3 生产库不在本次测试路径中。隔离数据库已删除，仓库没有保留
  一次性验收脚本。
- 2026-08-20 已使用冻结同源数据、隔离数据库和热缓存副本完成 DataBridge、daily、weekly、monthly
  与 Actuals 的 systemd 调度等价验收；三频 56 个 run、60 条预测全部成功，7 个 Liwei family 均走
  有限 suffix、full build 为 0，daily 墙钟约 62 分钟，低于两小时硬限。隔离资源已删除，生产库、
  cache pointer 和 release 未改变，五个 timer 继续自然灰度运行。
- 两端使用各自数据库、DataBridge 和运行记录，不复制、不双写、不共享运行期 authority；ECS Backend 仅监听 loopback，不承载生产公网流量。
- 仓库模板、代码和测试不能单独证明任一主机现场已加载或已运行；ECS 现场验收和后续自然监控也不
  自动授权 Web/Writer 切换。
- config active、exact version active、Registry target active 且 cadence 匹配，是进入对应 one-shot runner 的唯一资格。
- 自然运行写 `scheduled_live`；单日人工补缺只经 `python -m harness signal-gap-fill --predict-date YYYY-MM-DD` 写 insert-only `gray_live`。
- Blackbox Admission、Backend 手动预测、direct scheduling、ledger、occurrence、epoch、daily-gray 和常驻 APScheduler 均已退役。
- installed plist/unit/timer、launchctl/systemctl 服务变更、激活、持久化回测、额外业务写入和 DDL 仍是独立操作，必须获得明确授权。

## 当前迁移完成边界

- ECS 独立灰度迁移已经完成。
- 双主机单一 source release 代码线治理、分阶段晋级、R2 外置环境合同、ECS 精确验证和 Mac3 immutable release
  现场切换均已完成，本轮部署治理迁移已经闭环。
- 生产域名或 Writer 改切 ECS 是未来可选项目，不属于本轮完成条件，也未由本次操作授权。

## 当前输入权威

- Native 只使用当前权威 `bond_db`，按 `predict_date` 推导的 `feature_date` 截止，经 `shared.input_artifacts` 构建输入。
- source-backed Native 连接同一 MySQL 实例和同一 `bond_db`，仅使用独立 SELECT-only 身份；它不是第二套数据库或输入链路。
- Blackbox 使用 DataBridge current snapshot；历史补缺严格绑定冻结的 DataBridge authority。
- Native 与旧 DataBridge 的二级 generation 运行控制面已退役。`t_input_generations`、migration 017 和历史行仅保留为历史 schema/审计证据，不再参与 Native 运行资格或输入构建。
- Phase-A manifest 继续兼容 `native_generation=null`、`native_generation_changed=false` 和 `NON_PRODUCTION`，不再支持 bind/rebind。
- Phase-A 只复用 manifest v3 current generation；current 缺失时由 publisher 按当前输入完整重建，旧松散 v1 `.pkl` 不再作为迁移来源。普通同结构数据值修订只允许在 canonical dependency proof 下重算经验证的有限 suffix；算法、spec、ABI、schema、日期删除或交易日结构变化仍 full/fail-closed。已经写入数据库的历史业务预测不因事后数据修订而回写。

## 当前文档与评审边界

- 当前规则只由根规范、CURRENT 架构/契约、Onboarding、SOP、生产准备清单和运维/产品手册定义。
- 已实施计划、已关闭交接和被后续规则替代的决策草案不保留在工作树；需要追溯时使用 Git、Harness、run/prediction 和数据库审计。
- 仓库不保留一次性 ECS/迁移验收测试；release、launchd、systemd、suffix/cache 和补数测试是长期
  回归合同，必须随对应生产能力保留。

## 当前 Harness 合同

- 首次技术入库 `all` 固定为六段 `static -> input -> unit -> dry-run -> compare -> backtest`；`native-maintenance` 固定为五段 `static -> native-maintenance-admission -> input -> unit -> dry-run`。两者都不访问 Backend。
- 激活后只用 `dashboard` Gate 校验 `/api/factor-lab/dashboard` 的当前业务快照；该 payload 不携带 exact version，因此不能用来证明 exact version。
- `signal-gap-fill` 只支持单个 `predict_date`，可选限定一个 base scheme；命令执行权本身就是补数授权，不另设用户、token、确认或 plan SHA 层。Native 从当前数据库重建，Blackbox 重放冻结 DataBridge authority；所有算法成功后才按 group insert-only 写 `gray_live`，最后执行一次权威读回。
- Blackbox lifecycle 存在 pending 时直接阻断后续操作；只有显式、独立 HMAC 授权的 `lifecycle-reconcile` 可修改状态，其它 Gate 不做隐式恢复。
