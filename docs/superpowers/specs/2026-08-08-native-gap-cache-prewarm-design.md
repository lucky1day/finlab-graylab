# 封存 Native 快照的受控 Phase-A Cache 预热设计

## 目标

为历史 `gray_live` 信号回填提供一个最小的、可审计的 Native Phase-A cache 预热步骤。它只解决封存
Native 输入已存在、但依赖 cache 的 consumer 因 `hit_only` 找不到精确 generation 而不能回填的情形。

本次直接适用的业务键是：

- consumer：`liwei_0616_10y01_cons_say_k3_div_k10`
- publisher：`liwei_0616_10y01_full_oos_k3_div_k10`
- `predict_date=2026-08-05`、`feature_date=2026-08-04`、`target_date=2026-08-11`

## 已确认的根因

回填运行正确地把 Native cache 固定为 `hit_only`，且只接受与封存输入 generation 完全相符的
`current.json`。生产实时 cache 属于另一个根目录、没有封存 generation binding，不能安全复用；封存
artifact 对应的专用根目录中则尚未发布 publisher generation。因此 consumer 返回
`SIGNAL_GAP_CACHE_PREWARM_REQUIRED`，预测记录没有写入。

## 方案选择

采用下列闭环：

1. 只读的 `signal-gap-native-artifact prewarm-authority` 先重验数据库中的精确 `SEALED` 登记，
   再从封存 Native artifact 导出预热 HMAC 所需的完整 authority；
   `signal-gap-native-artifact prewarm` 会再次重验该 fence，并只允许唯一的 10Y publisher
   `liwei_0616_10y01_full_oos_k3_div_k10` 执行。
2. cache 根目录固定派生为 `<artifact-storage-root>/.phase-a-cache/<generation-id>`；不接收任意运行时
   cache 路径，也不读取实时 cache。
3. HMAC 校验与消费后，父 harness 在该派生根签发一个 `0600`、短时、一次性的子进程 permit；permit
   绑定完整 Native generation、唯一 publisher、派生 cache root、HMAC token 摘要和到期时间。executor
   只把 permit 路径及随机 capability 传给子进程；cache 写入点会先验证并原子消费 permit，才允许建立和
   发布 `current.json`。它不创建 `t_scheme_runs` 或预测记录。
4. 既有 `signal-gap-fill` 对 current-snapshot Native group 自动使用同一派生根目录，且仍使用
   `hit_only`。consumer 只读、重新校验 cache 的 Native generation binding、输入状态和覆盖范围，然后才
   写既有的 `gray_live` 业务键。

预热授权是独立的 HMAC action，TTL 不超过 900 秒，绑定 publisher、historical predict date 和完整的
封存 artifact authority。预热失败即停止；不会降级到实时缓存、不会让 fill 自行构建 cache，也不会把
HMAC 或其密钥传给算法子进程。

## 信任边界

该 permit 是现有受信任 harness → executor → Native 子进程链中的一次性 capability handoff：它防止
普通 scheduler/环境变量路径意外进入 prewarm，也约束受控操作员必须先通过 HMAC、精确 `SEALED` fence
与唯一 publisher 校验。它不是对“同一 macOS 用户可任意运行 Python 并改写可写 cache 文件”的密码学
防线；当前 cache 也必须由该用户的算法子进程写入。若未来需要抵御这个更强对手，应另行设计不同 UID 的
特权 writer/broker 或固定公钥验签，不在本次单条历史信号修复中引入。

## 明确不做

- 不启动 DataBridge、launchd、scheduler 或后台服务；
- 不增加数据库表、ledger/occurrence/epoch、第二条调度管线或缓存回退；
- 不重跑已完成的 Blackbox gaps；
- 不伪造 `scheduled_live`，只由既有 Gate 写缺失的 `gray_live`；
- 不新增一组 SHA 比对；只使用 artifact/cache manifest 已有的身份字段和既有 cache 校验。

## 失败语义与验收

- artifact、授权、publisher 身份或 cache 发布不满足条件：prewarm 返回明确 failure code，fill 不执行；
- publisher 成功：返回已发布 cache audit，且 audit 的 Native generation 与封存 artifact 相同；
- fill 成功：只写授权的一个业务键，`records_written=1`；
- 最终只读 gap report 为 `missing=0`，Dashboard/API 仍是实时数据库读。
