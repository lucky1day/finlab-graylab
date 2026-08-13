# Blackbox V2 方案名称与交付来源设计

## 目标

所有正式新 Blackbox V2 方案必须同时提供三类展示信息：

- `name`：候选排行中的方案名称；
- `owner`：候选排行“来源”列中的交付同事姓名缩写、姓名或稳定团队代码；
- `description`：候选排行“备注”详情中的算法说明。

本阶段先更新算法交付 SOP 与本地灰度实验室入库 SOP，供算法同事准备新交付。机器 Contract、Intake 原子登记与 Gate 守护在后续独立实现。

## 选择方案

采用方案 A：`owner` 进入上游 `{scheme_id}.json`，平台 Intake 校验后按 composite Registry ID 自动登记到 `deploy/scheme_owner_v1.json`。

不采用只靠人工登记的方案，因为交付文件与前端来源可能长期断开；也不采用 `--owner` 命令参数，因为交付文件本身将无法证明其来源。

## 字段职责

| 字段 | 权威来源 | 展示用途 | 是否参与交付文件摘要 |
|---|---|---|---|
| `scheme_id` | Metadata | 机器执行身份 | 是 |
| `name` | Metadata | 前端“方案”列 | 是 |
| `owner` | Metadata；Intake 后登记到平台 owner registry | 前端“来源”列 | 是（仅新交付） |
| `description` | Metadata | 前端“备注”详情 | 是 |

`owner` 表达方案交付归属，不是 DataBridge 数据源、`input_source`、算法依赖来源或审批人。`name`、`owner`、`description` 不得互相代替。

## 正式新交付规则

正式新交付的 Metadata 必须包含历史八字段以及 `name`、`owner`、`description`。其中 `name` 已属于历史八字段，文档需要把它从“机器非空”提升为明确的业务展示要求。

`owner` 必须是去除首尾空白后仍非空的单段纯文本，不得包含换行、HTML 或其他标记文本。允许姓名缩写、姓名或稳定团队代码；不得填写 `--`、`unknown`、`待定` 等占位值。

## 历史兼容

- 已入库的不可变 V2 Metadata 允许缺少 `owner`；不得为补来源原地修改交付文件。
- 历史来源继续在 `deploy/scheme_owner_v1.json` 中补录。
- 新规则生效后，新 Intake 不提供 owner waiver。
- 若已入库方案需要改变 `owner` 所代表的交付归属，只修改平台 owner registry；若修改 Metadata，则属于新交付版本并按受控 revision 处理。

## Intake 目标流程

```text
{scheme_id}.json
  ├─ name ──────────→ SchemeConfig.name → Registry.name → 前端方案列
  ├─ description ──→ Registry.description → 前端备注详情
  └─ owner
       ↓ Intake 校验
{scheme_id}__h{horizon}__{target_tenor}
       ↓ 原子登记
deploy/scheme_owner_v1.json
       ↓
Dashboard.owner → 前端来源列
```

Intake 必须在任何文件写入前完成 Metadata 与 owner registry preflight。相同 composite ID 和相同 owner 可幂等接受；相同 composite ID 和不同 owner 必须 fail-closed，不能覆盖。交付保存与 owner 登记必须形成单一成功或单一失败结果，不得留下半写状态。

## 文档先行过渡

当前机器 Contract 尚未接受 `owner` 字段，因此文档提交后到配套机器变更上线前：

- 算法同事按新 SOP 准备包含 `owner` 的两文件包；
- 平台可以做人工内容核对，但不得运行旧 `intake-blackbox`；
- 平台不得删除 `owner` 后代收，也不得把 owner 改放命令行或平台私有交接材料；
- 配套机器变更上线并验证后，平台再按新 SOP 执行 Intake。

这段过渡防止新包被旧 Contract 以“extra field”拒绝，也防止平台为了兼容旧实现破坏方案 A 的来源闭环。

## 本阶段文档变更

1. `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`
   - 更新 Metadata 示例、字段约束、自验表和最终交付清单；
   - 明确 `name`、`owner`、`description` 的展示职责；
   - 加入文档先行过渡说明。
2. `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`
   - 更新接收前检查、Contract 对照、目标传播链路、Gate 与前端验收；
   - 明确 owner registry 冲突和缺失时 fail-closed；
   - 加入旧 Intake 暂停执行说明。

本阶段不修改算法脚本、Contract 解析器、Intake、Gate、数据库、Registry 或前端代码。

## 后续机器实现验收

后续实现至少覆盖：新 Intake 缺 `name`、`owner` 或 `description` 失败；owner 非纯文本失败；owner 原子登记；同值幂等；异值冲突；失败无半写；历史缺 owner Metadata 仍可发现；Dashboard Gate 对新激活方案要求非空 `name`、`owner`、`description`。
