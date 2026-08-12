# 方案归属同事展示设计

## 目标

前端「候选方案排行」表格新增一列「来源」，显示该方案由哪位算法同事交付，用姓名缩写表示（不落个人身份信息）。

## 不做什么

- 不改任何 `scheme_id`、`base_scheme_id` 或 registry composite ID
- 不改任何 `scheme_version`、`config_hash`、`code_hash`、`manifest_hash`
- 不新增数据库表或列，不做 migration
- 不做筛选、分组、排序等派生功能；只是一列展示

## 硬约束（实测得出）

归属信息**不得写入任何参与版本哈希的文件**：

| 文件 | 进哪个哈希 | 后果 |
|---|---|---|
| Native `config.yaml` | `config_hash`（整文件字节） | `scheme_version` 变 → `_native_sync_can_grandfather_active` 返回 False → 26 个 active Native registry 行在下次 backend 同步时被降级为 `paused`，从前端消失 |
| Blackbox 交付 `{id}.json` | `manifest_hash`（整文件字节） | `scheme_version` 变 → 触发 `_blackbox_identity_requires_controlled_revision_conn`，需走受控 revision-activate |
| Blackbox `config.yaml` | `config_hash`（**固定白名单键**） | 加未知键不改哈希，但与上面两条不一致，不作为统一载体 |

因此**存量方案的归属只能记录在不参与哈希的位置**。

## 数据模型

新增版本控制文件 `deploy/scheme_owner_v1.json`：

```json
{
  "schema_version": "scheme-owner-v1",
  "owners": {
    "weekly_10y_lgbm_point_v1__h1__10Y": "LW",
    "cgb_a4_fundseason_10y__h1__10Y": "ZS"
  }
}
```

- **键**：registry composite `scheme_id`。registry 每一行就是一个前端业务方案，排行表的每一行也是 composite 方案，两者一一对应。
- **值**：姓名缩写字符串。同一算法的多个期限各占一条；这是 composite 粒度的固有结果，不做前缀推导或继承。
- 该文件是平台侧的唯一读取来源。

选择版本控制文件而非 registry 新增列的理由：

1. 归属是平台对方案的**登记信息**，不参与任何计算、gate 或 join，不应进入执行身份
2. 真值留在 git 里：可 PR 审查、有变更历史、新环境自带
3. 无 migration、无 DB 写入授权、无 `scheme_version` 变动
4. 已有先例：`deploy/onboarding_policy_v1.json` 就是版本控制的按 scheme_id 策略文件

## 数据流

```
deploy/scheme_owner_v1.json
        │  (loader 读取，按 composite scheme_id 查表)
        ▼
backend  → dashboard payload 的 scheme 增加 owner 字段
        ▼
frontend → 排行表新增「来源」列
```

后续新方案的归属在**交付时由算法同事在交付 JSON 中声明**，平台入库时把它登记进本文件（SOP 需相应修订）。这样新方案的归属随交付一起进版本库，且首次入库即计入 version，不产生 churn。

## 组件

### 1. `deploy/scheme_owner_v1.json`

存量条目由用户一次性统计填写。

### 2. Loader（`backend/` 内，与 dashboard 同层）

- 读取并校验 `schema_version`
- 返回 `dict[composite_scheme_id, owner]`
- 文件缺失或格式非法：fail-closed 抛错，不静默返回空表

### 3. Dashboard payload

- `SCHEME_FIELDS` 白名单新增 `owner`
- 每个 scheme 的 `owner` 取自 loader；**未登记的方案取值为空字符串**

未登记不报错，因为存量回填与新方案入库之间存在时间差；但登记缺失应可见（前端显示 `--`），不伪装成已登记。

### 4. 前端

- `frontend/index.html`：表头在「部署时间」与「备注」之间插入 `<th>来源</th>`
- `frontend/aifin-shell.js`：
  - `renderSchemeRankingRow` 增加对应 `<td>`
  - 空态 `colspan="8"` 改为 `"9"`
  - dashboard scheme 解析处透传 `owner`
- `index.html` 中 `aifin-shell.js` 与 `aifin-shell.css` 的内容哈希查询参数需同步更新

## 失败模式

| 情况 | 行为 |
|---|---|
| 映射文件缺失 / `schema_version` 不符 / JSON 非法 | fail-closed，dashboard 构建报错 |
| 某方案未登记归属 | payload `owner=""`，前端显示 `--` |
| 文件中存在 active registry 里不存在的 scheme_id | 忽略；该条目不影响任何展示 |

不为「文件读不到」提供空表回退——那会让「全部未登记」与「配置坏了」不可区分。

## 测试

1. loader：正常解析、`schema_version` 不符 fail-closed、JSON 非法 fail-closed
2. payload：已登记方案带 `owner`；未登记方案 `owner=""`；`SCHEME_FIELDS` 契约测试覆盖新字段
3. 前端契约：表头列数与 `renderSchemeRankingRow` 的 `<td>` 数一致；空态 `colspan` 与列数一致
4. 资产哈希：`test_frontend_asset_versions.py` 覆盖 JS 改动后的哈希同步

## 后续（本设计不含）

修订 `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`，要求交付 JSON 声明 owner，并在 intake 阶段校验。该项独立提交。
