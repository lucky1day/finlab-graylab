# 运行信号与调度治理

**文档状态**：`CURRENT`

本文定义自然 Writer 权属、现场证明、手工恢复和停止条件。期望日历、模板、release 与迁移恢复见
[部署手册](../../deploy/README.md)，主机连接见[访问入口](../operations/DEPLOYMENT_ACCESS.md)，
已核验基线与未完成观察分别见[当前状态](../CURRENT_STATUS.md)和[TODO](../TODO.md)。

## 唯一控制面与现场证明

每个部署目标和数据库 authority 内，每个 cadence 只有一个自然 Writer：Mac3 使用 launchd + installed
plist，ECS 独立灰度使用 systemd + installed unit/timer。Python runner 只是宿主调用的一次性执行器。
预检、手工调用或环境标记不能单独证明自然触发；退役控制面禁止重建，见[根规范](../../AGENTS.md#环境与发布)。
历史调度数据库对象仅用于审计或受控 recovery，不能重新取得 Writer 权限；物理清理须单独设计和授权。

自然运行须联合核对：

1. 仓库期望模板、installed 配置、loaded state 和真实触发时间；
2. 进程实际 cwd 与精确 release，启动环境及部署目标；
3. 同次日志、run、prediction、exact、三个日期及本机输入来源；
4. Dashboard 与已发布事实一致；它不携带 exact，不能代替数据库版本读回。

只有上述证据一致才可标记自然运行成功；仅有配置文件、环境标记、补跑或人工模拟均不足以证明。
DataBridge 的 `BFL_DATABRIDGE_PRODUCER` 在 Mac3 为 `launchd-one-shot`、ECS 为 `systemd-one-shot`；
它只是防误 publish 的准入标记，不是宿主身份认证。同 UID 调用者属于受信任边界，仍须上述现场证据。

canonical、数据库生命周期与部署的状态区分见[共享生命周期契约](SCHEME_CONTRACT.md#7-生命周期与分派)。自然候选先按部署矩阵与 cadence 过滤，再解析本机生效生命周期。Blackbox 以本机 exact version 与 composite Registry 为准，canonical 的初始 paused/draft 不能提前排除已激活方案；W4 Native 仍使用固定配置状态。收盘预规划与 one-shot 执行复用同一解析规则，执行前与事务内分别复核；数据库不可读或身份不一致时停止，不回退文件状态。不维护第二份 Admission、release queue、mode 或 capability 权限矩阵。

close-period 复用原 monthly 控制面，按[预测语义](PREDICTION_SEMANTICS.md)选择到期月中收或周期均值任务，
普通日期 no-op；期望时钟见部署手册，不能因存在 active 方案推断当天必须执行。

## 自然信号与手工恢复

自然触发的合格写入使用 `scheduled_live`；受控历史补缺使用 `gray_live`。这两者是 run 来源审计，
不得互相伪装或以日期标签替代 provenance。产品历史/实盘分界及不可变回测事实见预测语义。

历史缺口只经[平台 SOP 的单日/区间入口](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#5-可选后续动作)修复；支持范围、参数、跳过/重复键行为和输入绑定都在那里维护。先确认授权覆盖的方案与日期、已发生副作用及实际剩余缺口，不把一次失败当作全部未写入。

当天自然 one-shot 部分失败后的受控 `scheduled_live` 重试可向现有 launchd/systemd runner 重复传入
`--scheme-id <base_scheme_id>`，只缩小 active cadence 候选，不绕过部署、Registry、exact、日历、输入或
insert-only 检查。runner 的 `--predict-date` 不是第二套历史补缺授权，不能把历史点重标自然运行。
维护跨越正式触发点时须核对是否已执行；遗漏历史点走上述受控补缺，不伪造时钟证据。

普通 active completion 的 benign `skipped` 是算法已经执行、返回 records 并通过写前复核后产生的发布结果，不是调度 preflight skip，计算成本已经发生。one-shot batch 的退出码 `0` 仅表示没有 actionable failure，同一摘要可以同时包含首次发布 `success` 与完整重复 `skipped`，不能据此声称没有执行方案。区间补缺拒绝已有键的语义仍按平台 SOP，不转换为此类 skipped。

跨 cadence 的 one-shot Writer 继续使用 runtime 下同一路径的 `flock`，但只以
`LOCK_EX | LOCK_NB` 有界轮询；launchd/systemd runner 的 `--lock-timeout-sec` 默认 60 秒。期限内未取得锁时
返回 `stage=runner_lock`、`code=lock_wait_timeout`，不得归为数据库不可用，不删除锁文件，也不自动终止持锁
进程。抢到锁后再次检查本次等待期限，并在锁内重新执行原有日历覆盖、交易日和周期到期判断；始终保留调用方
传入的 `predict_date`，错过自然窗口时不改成新日期或自动补跑历史。

批次从创建后始终沿用同一个 `OneShotPredictionSummary`。方案成功后发生依赖、锁后配置或资源收尾异常时，
外层回执必须保留已经完成的 `executed`、`run_id` 与 `records_written`，并追加带 `scheme_id`、可用
`run_id`、`stage`、`code`、`records_written` 的失败项；不得用新的空 configuration summary 覆盖，也不自动
重跑摘要中已完成的方案。批次级失败没有对应方案时 `scheme_id` 为空字符串。

## 输入新鲜度

算法执行前必须通过本机输入 ready 与截止校验；失败不得降级旧 artifact 或制造成功信号。源表、schema、
连续性、稳定轮次和发布身份的完整检查见[DataBridge](../blackbox_v2/data_bridge_v1/README.md)。

日级 ready gate 只把“当次凭证尚未出现”或显式 `checking` 视为可在既定期限内等待的状态。`blocked`、
凭证 JSON 损坏、schema/run date/feature date 不匹配、current 合同损坏及稳定的 digest/日期不一致均以稳定
错误码立即失败，不从异常文本推断类别。ready 与 current generation 暂时不一致，或 ready 校验瞬间 current
暂缺，只允许三次短间隔连续重读；仍不一致即失败，不进入最长 30 分钟的普通等待。普通等待每五分钟输出仅含
事件、级别、`elapsed_sec`、`remaining_sec` 和 `code` 的结构化进度，不含底层异常文本，并从五分钟起标记
warning，供控制面提前告警。producer 从 publish 开始到 retryable 重试结束前始终写 `checking`，成功才写
`ready`；非 retryable 终止或达到 refresh deadline 后才写 `blocked`。

close-period 预测前核对 current ready 是否精确覆盖所需锚点：覆盖则月中收与周期均值顺序复用同一已发布快照；
未覆盖只做一次收盘刷新再核验，失败或截止不符则该批预测零执行。它是原 monthly 入口前置动作，不新增
timer 或第二 DataBridge Writer。

## 授权、生命周期与停止条件

变更前按根规范取得对应授权，并先核对本页列出的现场证据、在途任务和恢复范围。

Blackbox 的 Intake、同 exact/当前校验策略持久化回测与激活条件见[平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)；
W4 固定版本的运行检查与故障恢复见[W4 运行手册](../sop/NATIVE_V1_MAINTENANCE_SOP.md)，不再进入 Native 新版本准入。
技术 Gate 不等于安装控制面或自然观察。DB 生命周期成功后仍须完成目标机调度与产品读回。

以下情况停止副作用、保留证据并说明需确认的事项：

- active scope、exact、Registry identity、输入截止或日期语义与预检不一致；
- installed/loaded、日志、run 和 prediction 无法证明同一次自然运行；
- 两个 Writer 可能写同一业务键；
- 需要 fallback、未授权切旧版本、覆盖、自动重试或恢复已退役控制面才能继续。

恢复源码、schema 和外置状态前执行[部署恢复](../../deploy/README.md#回滚与认证部署)的兼容性与备份检查。

## 方案输入质量审计与每日结果

数据质量检查采用**只记录、继续计算**。输入存在缺值、错期、窗口不足或审计声明未核验时，平台记录详细问题后执行原模型，不在平台填0、沿用旧方向或替换输入。模型能计算时正常发布其原始方向；模型自身无法计算时，保留当日failed run及运行日志，并记录`prediction_result.outcome=not_computable`、`predicted_direction=null`。不向预测事实表插入虚构的0或旧预测。真实休市/非到期任务仍按原日历no-op；不能确定日历或无法取得可信输入时，按已知active候选逐模型记录无法计算，不能假装成功或猜测业务日期。

`deploy/prediction_input_quality_v1.json`是静态审计目录，覆盖部署矩阵中的全部执行文件，绑定base、exact和脚本SHA；profiles维护检查窗口与输入角色，运行方式统一为audit。绑定漂移明确记`policy_mismatch`，未声明记`not_declared`，检查器异常记`check_error`，均不能声称输入完整。它不取代原有执行身份、Registry、ready快照或生命周期校验，也不成为第二份启用名单。

Blackbox在已取得可信runtime view和Request后审计封存文件，再启动原交付。日、周、月使用各自的精确cutoff；只在cutoff及以前统计窗口，未来行不能补足历史。所需列、窗口、缺失日期、有效值/有效差分数量及最近可用观测逐项留证，合法0不会被当成空值。多目标按执行文件分别记录；原算法和exact不变。

Mac3 W4九套固定版本通过独立审计子进程从原legacy DB经`shared.input_artifacts`导出核查，不改接DataBridge、不改adapter/core或源包。审计最多30秒，之后原模型拥有原有完整计算预算；审计失败或超时只记录，不吞取消/进程组清理异常。Native源包未完整公开逐因子依赖时，审计明确标为“导出输入集上界”，不能把所有导出列说成模型必需因子。审计期间源水位变化单列；水位不是全值digest，也不证明二进制算法最终使用的每个值。

### 日志与结果关联

完整结构化日志追加到`BFL_RUNTIME_ROOT/logs/prediction-input-quality/YYYY-MM-DD.jsonl`（Asia/Shanghai记录日），以run_id、audit_id、scheme、执行文件、预测日和snapshot关联。线程锁与flock均非阻塞；竞争或不可写时转存完整stderr，成功追加后fsync。Native父进程仅转发本审计协议行，不转发未识别的算法stderr或连接信息。日志sink异常不把预测成功反转为失败。

多目标和区间预测仅在单次封存runtime view内复用策略、CSV及执行文件摘要，逐Request重新核查窗口与截止日；不跨批次缓存结论，不把不同目录或不同策略混入同一批次，也不修改算法输入。

- `prediction_input_quality`：状态、输入覆盖范围、规则/版本身份、各窗口边界、缺列/缺值日期及最近可用值；明确平台未填数。
- `prediction_result`：发布状态、原始模型方向和审计引用；无法计算时方向null及失败原因，同时保留既有run/run_log。
- `prediction_input_unavailable`：原ready gate错误分类；无法取得可信快照时不制造方向，逐模型留下当日无法计算记录。
- `native_input_audit_source_observation`：审计期间的源水位观察，不能标成算法消费的精确快照。

`last_available_observation`只表示窗口之前最近可用观测，**不能写成模型实际使用值**。交付未暴露内部填补行为时，记录`actual_value_treatment=not_exposed_by_delivery`及“原模型原生处理”，不猜测它填了0、使用旧值或跳过因子。未来如需精确到内部选值，应由交付提供可核验的审计接口；平台不篡改冻结算法追踪结果。

所有人工核查保留其人工来源，不冒称自然运行。已发布事实仍遵守insert-only；新增日志不会触发旧预测重算。

2026-10-29当日整批替换是已经闭环的独立修复，未来release不再提供该一次性改写出口。
历史修复脚本、回执和对应immutable release仍用于追溯，不能作为现行操作入口。
现行已发布事实保持insert-only；输入审计和失败日期追补不构成覆盖历史的授权。
