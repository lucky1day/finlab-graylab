# 当前状态

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-20

本文只记录当前稳定事实。实时方案、run、prediction、DataBridge、API 和调度状态必须从各自权威数据源读取，不在仓库文档冻结数量、运行 ID 或 Git SHA。未批准工作见[统一后续推进计划](TODO.md)，生产调度规则见[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 当前双主机运行规则

- Mac3 继续承载生产域名、前端、数据库和 Writer；`launchd + installed plist` 是 Mac3 的自然生产调度控制面。
- Mac3 immutable release R2 已冻结为 tag `mac3-immutable-r2-20260820`，精确 commit 为
  `e692285d47e41c384dc915758abe0c51f9ac3aaf`，archive SHA256 为
  `9b414792f51f5a47fda46ae403dfdf5f8909fdebe511512fd4cead36f666dac4`。其仓库能力已收敛为独立
  `bond-factor-lab-production/current`、外置 `bond-factor-lab-runtime` 和最小 launchd release
  launcher；两次确定性构建、全量回归、独立审查和 ECS 精确 archive 晋级读回均已通过。ECS 当前
  `current=e692285d47e41c384dc915758abe0c51f9ac3aaf`、
  `previous=c4e15eb9fbf0a278a961a7dce4d3a25b9394a724`。Backend 与 DataBridge check-only 已通过，五个 timer
  保持 enabled/active。这些操作均未改变 Mac3 installed plist。
  当前六个 Mac3 应用 plist 现场仍绑定 `/Users/macstudio0/bond-factor-lab`，开发根工作区仍为
  `codex/audit-bugfixes-20260613`，因此尚不能切换该工作区分支。
- 2026-08-20 Mac3 已在不创建 `current`、不替换 plist 的前提下预安装 R1；备用端口候选验证证明旧
  Backend 隐式依赖 Git 根 `.env`，immutable release 因没有数据库凭据而 fail-closed。现有 8100
  Backend、前端和调度均未改变。R2 已实现从 runtime `config/service.env` 显式、安全加载本机配置；
  不把 `.env` 复制进只读 release，也不把全部 secret 展开到每个 plist。Git 根 `.env` 只服务开发工作区，
  `service.env` 只服务 Mac3 生产 release；
  两者仅首次迁移时复制一次，此后永久独立、永不自动同步，生产配置变更只在重启对应服务后生效。
- ECS 是独立灰度实验室，不是 Mac3 热备或复制节点；其 DataBridge、daily、weekly、monthly、Actuals 五个 systemd timer 已于 2026-08-18 经专项授权启用，现场 authority 是 installed unit/timer 与 `systemctl` 读回。
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
- 双主机单一 source release 治理的 R2 外置环境合同和 ECS 精确验证均已完成；下一步进入 Mac3
  独立窗口预安装并激活同一 R2、替换
  六个 installed 应用 plist，并独立替换 SSH tunnel plist（保留真实 key/user）、
  读回七个 loaded state、Backend/release identity，并证明应用进程与 tunnel 日志都不再引用 Git。
- 上述现场验收完成后，才能把 Mac3 开发根工作区切到 `codex/develop`；该时点即为本轮部署治理
  迁移闭环。生产域名或 Writer 改切 ECS 是未来可选项目，不属于本轮完成条件。

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
