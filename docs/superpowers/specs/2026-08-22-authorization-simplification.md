# 授权机制瘦身设计

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

操作者改为提供必须逐字匹配的显式确认串：

```
--i-authorize "<action>:<scheme_id>@<scheme_version>"
--issued-by <operator>
```

系统用当前 exact version 自行拼出期望串比对，不一致即拒绝。该串**不是凭据**，不需要保密，
它的作用是强制人写出精确身份——防的是操作错对象，不是防攻击者。

### 2. 去掉 900 秒有效期

删除 `AUTH_MAX_TTL_SECONDS`、`AUTH_MAX_FUTURE_SKEW_SECONDS`、`issued_at` / `expires_at` 及
`_timestamp_errors`。授权本就一次性且绑死 exact run，过期只增加"签发后必须 15 分钟内用完"的
纯摩擦。

`issued_by` 与授权发生时间仍写入审计记录。

### 3. 三段登记合并为一次操作面

**保留三段各自的内部事务与 lifecycle journal，只合并操作者面。**

`draft-register`（insert-only 建 draft version + paused registry）、`shadow-register`、
`activate` 三者的数据库效果和 journal 语义不变，仍按序执行、逐段可回滚。变化只有两点：

- 新增一个操作者命令一次驱动三段；
- 三段共用**同一个授权**，不再各签一次。

不采用"融合成单事务"：那会改变 lifecycle journal 语义和分段回滚能力，风险远大于收益。

### 4. 删除 `BOND_FACTOR_LAB_SERVICE_FINGERPRINT_SECRET`

唯一消费者是 `backend/main.py` 通过 `service_fingerprint_secret()` 构造 `/api/health` 的
`service_instance` 信息字段；未设置时为 `None`，现场实证即为 `None`。

删除该 env、`shared/service_instance.py` 中的相关分支，并同步调整 `/api/health` 响应结构与
`tests/test_backend_api.py`、`tests/test_service_instance.py`。

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
   错 run、错 predict_date、错 action）必须继续拒绝，只是构造方式从签名 token 改为确认串。
2. **一次性仍成立**：同一授权第二次使用必须拒绝；且沿用 `01d1fec` 后的外置重放存储，
   换 project_root 仍拒绝。
3. **确认串必须逐字匹配**：version 差一个字符、action 不符、scheme 不符均拒绝。
4. **fail-closed**：缺 `--i-authorize` 或缺 `--issued-by` 时拒绝，且无任何数据库副作用。
5. **合并命令**：三段中任一段失败时，已完成段的 journal 保留、后续段不执行，与分开执行时一致。
6. **health 响应**：删除 fingerprint 字段后，`/api/health` 契约测试同步更新并通过。
7. 全量回归对照纯净 HEAD，不得出现新增失败。

## 停止条件

- 需要放宽 scope 校验、一次性或 fail-closed 中的任何一条；
- 合并操作面需要改变 lifecycle journal 语义或分段回滚能力；
- 需要 DDL 才能完成任一项。
