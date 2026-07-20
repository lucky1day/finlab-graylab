# Blackbox V2 可选算法说明设计

**日期**：2026-07-20
**状态**：已获用户口头批准，待书面复核
**范围**：Blackbox V2 Metadata、Intake、Registry/API、因子实验室前端和两份 SOP

## 1. 目标

允许上游在 `{scheme_id}.json` 中提供简短的算法逻辑说明，供灰度实验室“备注”列展示和后续版本回溯。

`description` 是推荐字段，不是入库必填项。缺失说明不得阻断 Intake、Gate、激活或 scheduler。现有 Blackbox V2 交付不追溯补充、不修改只读交付文件，也不因本次能力升级产生新的方案版本。

## 2. Metadata 合同

保持 `schema_version="1.0"`。现有八个字段仍为必填字段，新增一个可选字段：

```json
{
  "schema_version": "1.0",
  "scheme_id": "one_y_t5_example_v1",
  "name": "EXAMPLE_MODEL",
  "description": "使用流动性指标和滚动窗口构建特征，通过分类模型判断未来5个交易日1Y国债收益率方向。",
  "algorithm_version": "1.0.0",
  "target_tenor": "1Y",
  "task_type": "T+5",
  "horizon": 5,
  "target_rule": "target_date_yield_vs_feature_date_yield"
}
```

校验规则：

- `description` 缺失时合法，解析结果为 `None`；
- 存在时必须为去除首尾空白后非空的字符串；
- 最长 300 个 Unicode 字符；
- 必须是单段纯文本，包含换行或 `<`、`>` 时拒绝；
- 未声明的其他 Metadata 字段继续 fail-closed；
- 不从脚本、方案名、任务格子或平台状态推断算法逻辑。

新增字段仍属于 Metadata bytes。上游后续修改 `description` 时，manifest hash 和 canonical `scheme_version` 随之变化，使说明与算法版本保持绑定。

## 3. 兼容与 Intake 行为

Metadata 解析器把字段集合拆成“八个必填字段 + `description` 可选字段”。因此：

- 已有、不含 `description` 的 `schema_version=1.0` 文件继续被发现和运行；
- 后续交付可以包含或省略 `description`；
- Intake 缺少说明时仍返回成功，但结果中的 `warnings` 增加一条稳定机器提示；
- Intake 提供合格说明时 `warnings` 为空；
- 空字符串、错误类型、超长或疑似标记文本属于提供了非法字段，必须拒绝，不能降级成“未提供”。

非阻断提示只用于推动上游完善交付质量，不进入 Gate 失败计数，也不授予任何生产权限。

## 4. 平台数据流

```text
delivery/{scheme_id}.json.description
  -> BlackboxMetadata.description
  -> SchemeConfig.description
  -> t_scheme_registry.description
  -> /api/schemes 与 /api/backtests/factor-lab
  -> 前端候选排行“备注”列
```

复用现有 `t_scheme_registry.description`，不新增数据库列或迁移。Blackbox Metadata 没有显式说明时，`SchemeConfig.description` 使用空字符串；平台不得继续生成 `Blackbox V2: {name}` 作为算法说明。下一次正式 Registry 同步可将这类旧占位说明收口为空，但不修改原始交付文件或方案版本。

API 保留 `description` 作为权威字段。前端 `getSchemeRemark` 在现有 `remark/note/notes` 兼容字段之后读取 `description`，最终统一以转义后的纯文本展示。缺失时备注单元格为空，不显示自动生成的替代文案。

## 5. 边界与安全

- `description` 只能解释算法逻辑，不能承载灰度状态、部署日期、授权结论或人工运营意见；
- 前端继续使用 `escapeHtml`，不得通过 `innerHTML` 注入未转义的 Metadata；
- `description` 不进入算法 Request，不影响预测、回测、actual join 或 scheduler；
- Registry/API 的 active、composite ID、任务格子与三日期语义保持不变；
- 本次不修改现有 Blackbox V2 JSON，不批量制造算法摘要，也不反编译黑盒脚本。

## 6. SOP 更新

上游算法交付 SOP：

- 在 Contract 1.0 示例和字段表中加入可选 `description`；
- 明确强烈建议提供，内容应简述主要输入、窗口或规则、模型类型以及方向形成方式；
- 明确缺失不阻断，但禁止平台代写或猜测。

平台入库 SOP：

- 记录可选字段校验、Intake warning 和 Registry/API/前端映射；
- 明确已有方案不追溯修改；
- 将备注展示加入 API 与前端验收清单。

## 7. 验证策略

1. Contract：旧八字段文件通过；九字段文件通过；非法 `description` 和未知字段失败。
2. Intake：缺失说明成功且返回 warning；提供说明成功且无该 warning；交付 bytes 仍原样保存并设为只读。
3. Discovery/versioning：说明正确进入 `SchemeConfig`；缺失时为空；修改 Metadata 说明会改变版本。
4. Registry/API：显式说明写入并由 active API、回测 API 返回；缺失时为空。
5. Frontend：候选排行展示说明并正确转义；缺失时保持空单元格。
6. Documentation：测试锁定 `schema_version=1.0`、可选而非必填、缺失不阻断及既有方案免迁移。

## 8. 完成条件

- 新旧 Contract 1.0 Metadata 均按上述规则运行；
- Intake warning 可机器读取且不改变成功退出码；
- 显式说明端到端到达前端备注列；
- 无说明的既有方案继续正常运行且交付物不变；
- 两份 SOP、测试与实现保持一致。
