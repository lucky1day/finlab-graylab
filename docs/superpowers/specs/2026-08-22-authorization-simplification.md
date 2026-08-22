# 授权机制瘦身设计

**实施状态（2026-08-23）**：核心授权瘦身已按进一步简化后的单维护者模型实现。最终实现比
初稿更短：副作用子命令本身就是意图，不再增加 `--i-authorize` 确认串；exact version 与 latest
passed run 由系统绑定，operator 默认取环境或 OS 用户。为保持失败隔离，没有增加跨 shadow、回测、
activate 的万能命令；service fingerprint 与本次入库授权无关，继续留在范围外。

## 背景

当前副作用操作（activate、backtest_persist、live、三段 register 等 8 个 action）需要一个用
`HARNESS_AUTH_SECRET` 签名的一次性 HMAC token，绑定 `scheme_id / action / scheme_version /
harness_run_id / predict_date`，900 秒过期。

现场核实（2026-08-22，ECS 灰度）：

```
HARNESS_AUTH_SECRET                          未设置
BOND_FACTOR_LAB_SERVICE_FINGERPRINT_SECRET   未设置
ADMIN_TOKEN                                  未设置
BOND_SCHEDULE_EXECUTION_TOKEN                未设置
/api/health -> {'fingerprint_version': '2', 'fingerprint': None}
```

即整套密钥装置在灰度实验室**没有产生任何保护**，只产生了一个阻塞：因为密钥不存在，激活不是
"受控"，而是"做不到"。

此外，`.used_authorization_tokens.json` 此前位于 gitignore 的 `reports/` 下，每个 release 出厂
即空，**一次性语义跨 release 不成立**；该缺陷已于 `01d1fec` 修复，但说明该机制的核心属性长期
未真正兑现。

## 第一性判断

token 中绑定的 `scheme_id`、`scheme_version`、`harness_run_id`、`predict_date` **系统全部可以自己
推导**——它们就是刚通过的那次 `all` 在 `t_harness_runs` / `t_harness_gate_results` 中的记录。

真正不可推导的只有**一个比特**：「有人决定让这个精确版本开始写业务数据」。

因此现状是：**一个密码学信封，包着一堆可推导数据，只为传递 1 bit 意图。**

而且威胁模型不成立。执行激活者持有 ECS root SSH，数据库口令就在同一台机器的 root-only 文件中，
可直接 `mysql` 写 `t_scheme_registry`。HMAC 拦不住任何 root 拦不住的事，只对守规矩的操作者收费。

## 目标

在**不降低**以下性质的前提下，去掉密码学机制：

- 精确身份绑定：不能激活错方案或错版本；
- 一次性：同一授权不能被重复消费；
- 审计：谁在何时授权了什么，必须留痕；
- fail-closed：任何不匹配都拒绝。

## 范围

### 1. 去掉 HMAC 签名与密钥

`Authorization` 数据结构、`verify_authorization` 的**全部 scope 校验**（action / scheme_id /
scheme_version / harness_run_id / predict_date / backtest_start_date 精确匹配）保持不变。
删除的是信封本身：`_auth_secret()`、`_sign()`、`_decode_envelope()`、`_validate_canonical_signature()`
以及 `AUTH_SECRET_ENV`。

操作者直接执行精确的副作用子命令。子命令名已经表达 action，`--scheme-id`、日期、回测起点等
参数已经表达目标；再要求抄写一遍 `<action>:<scheme>@<version>` 仍是重复输入，并会重新引入拼写
和复制错误。CLI 从 canonical config 解析 exact version，Gate 从控制面选择 current exact version 的
latest passed run，并在执行前逐项复核。operator 默认取 `BFL_OPERATOR_ID` 或 OS 用户，可用
`--operator` 显式覆盖；它是审计身份，不是凭据。

内部仍生成 canonical、随机的 operation id，但该值不暴露在用户操作面。它只用于并发安全的一次性
消费，审计仅保存 SHA-256、完整 scope 和 `authorization_mode=direct_operator_command_v1`。

### 2. 去掉 900 秒有效期

删除 `AUTH_MAX_TTL_SECONDS`、`AUTH_MAX_FUTURE_SKEW_SECONDS`、`issued_at` / `expires_at` 及
`_timestamp_errors`。授权本就一次性且绑死 exact run，过期只增加"签发后必须 15 分钟内用完"的
纯摩擦。

`issued_by` 与授权发生时间仍写入审计记录。

### 3. 压缩正常路径，但保留提交边界

`shadow-register` 已经在首次身份不存在时自动完成 draft identity 创建，因此正常流程不再单独运行
`draft-register`。完整回测与 activation 继续是两条独立命令，因为它们修改不同业务对象、使用不同
事务和失败恢复语义。删除 HMAC 后，每条命令已经没有签发往返；继续合并只能少两次命令，却会让
回测失败、lifecycle pending 和 activation 回滚混在一个长操作里，降低稳定性。

最终最短路径是 `intake -> all(4 gates) -> shadow-register -> backtest --persist -> activate ->
gap plan/fill -> dashboard`。提速重点放在删除重复 Gate、自动推导 scope、只补真实 gap，而不是压平
有价值的事务边界。

### 4. 不改 `BOND_FACTOR_LAB_SERVICE_FINGERPRINT_SECRET`

该变量属于 release/service instance 识别，不参与 Harness 入库授权。即使 ECS 当前未设置，也不能
从“未启用”直接推出“应和授权重构一起删除”。为避免扩大变更面和混淆 `/api/health` 契约，本次不改。

## 范围外

**`BOND_SCHEDULE_EXECUTION_TOKEN` 不在本次范围。** 它不是授权密钥，而是 run 级别的幂等标识：
`t_scheme_runs` 有对应列与唯一索引 `uk_scheme_runs_execution_token`，`blackbox_v2_runner` 将其
转发给算法子进程，并有一条现存隔离断言
`test_native_rejects_blackbox_execution_token_before_subprocess` 要求 Native 在起子进程前拒绝它。

其数据库支撑属于已退役的 daily ledger schema，因此它确实是残留物，但清理它需要同时论证该隔离
断言在变量彻底消失后不再必要，并涉及 DDL——DDL 属独立授权操作。留待 ledger 残留物统一清理。

`ADMIN_TOKEN` 同样不在本次范围：它是 Backend 接口鉴权，与入库授权无关。

## 测试与验收

1. **scope 校验不回退**：现有 `verify_authorization` 的全部拒绝用例（错 scheme、错 version、
   错 run、错 predict_date、错 action）必须继续拒绝，CLI 正常路径自动构造内部 operation。
2. **一次性仍成立**：同一 operation 第二次使用必须拒绝；且沿用 `01d1fec` 后的外置重放存储，
   换 project_root 仍拒绝。
3. **用户面清理完整**：CLI 不再暴露 `auth`、`auth issue` 或 `--authorize`；副作用命令提供可选
   `--operator`，不要求秘密环境变量。
4. **fail-closed**：canonical config/version 缺失、latest passed run 缺失、scope 不一致、operation
   重放或 lifecycle pending 时拒绝，且无越界数据库副作用。
5. **审计不降级**：审计含 operator、scope、run、mode 和 operation hash，但不保存原始 operation id。
6. 全量回归对照纯净 HEAD，不得出现新增失败。

## 停止条件

- 需要放宽 scope 校验、一次性或 fail-closed 中的任何一条；
- 合并操作面需要改变 lifecycle journal 语义或分段回滚能力；
- 需要 DDL 才能完成任一项。
