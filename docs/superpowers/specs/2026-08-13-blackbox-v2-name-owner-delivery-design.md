# Blackbox V2 方案名称与交付来源设计

## 目标

所有正式新 Blackbox V2 方案必须同时提供三类展示信息：

- `name`：候选排行中的方案名称；
- `owner`：候选排行“来源”列中的交付同事姓名缩写、姓名或稳定团队代码；
- `description`：候选排行“备注”详情中的算法说明。

算法交付 SOP、本地灰度实验室入库 SOP 与配套机器闭环均已完成。当前 Contract 接受并校验 owner，正式 Intake 强制三项展示字段并原子登记 owner，StaticGate 与 DashboardGate 负责精确读回。

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

## 机器闭环状态

- 通用 Metadata 解析器接受 owner，并对空白、换行、标记文本和占位值 fail-closed；
- 正式 `intake-blackbox` 缺 owner 或 description 时在写入前失败；
- Intake 以进程锁串行 owner registry 更新，原子替换 registry 和 scheme，提交异常时恢复原 registry；
- 相同 composite ID + 同 owner 幂等接受，不同 owner 拒绝覆盖；
- 新交付不允许 `config.yaml.display_name`，名称只来自 Metadata；
- StaticGate 精确读回 Metadata owner 与 composite owner registry；
- DashboardGate 对新激活方案精确核对 `name/owner/description`；
- 历史缺 owner Metadata 仅按 `deploy/blackbox_v2_legacy_metadata_v1.json` 中的 base ID + Metadata SHA-256 兼容，任何字节变化都必须进入方案 A 新合同。

本实现不修改算法脚本、数据库 Schema、预测/回测逻辑或前端显示逻辑。
