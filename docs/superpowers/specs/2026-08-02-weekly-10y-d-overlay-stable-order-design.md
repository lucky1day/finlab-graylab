# Weekly 10Y D-overlay 跨年排序确定性修正设计

**文档状态**：`APPROVED_FOR_IMPLEMENTATION_PLANNING`

**目标读者**：平台维护、算法审计和生产运维人员

**最后核验日期**：2026-08-02

## 目标

修复 `weekly_10y_d_overlay_0529` 在 `predict_date=2026-08-01` 无法生成信号的
跨年排序不确定性，使同一受治理输入在不同滚动窗口边界下产生一致的 Model2 标签。

本次是用户明确批准的 Native V1 L2 微修正例外。修改仅限一个排序键和相应回归
测试；不更换模型、不改变特征集合、窗口、分段、投票、fallback、输入来源、写库或
前端展示。开发分支验证不等于生产激活。

## 已确认事实

- `202553` 与 `202601` 都被原始 `week_id_to_date` 公式映射为 `2025-12-29`；归档
  source 和当前 adapter 均保留该映射。
- Score 以 `week_id` 的严格时间顺序计算下一周 label。
- Model2 在标签、lag 和差分特征生成前只按 `date` 调用 pandas 默认排序。
- pandas 默认 quicksort 对相同排序键不保证稳定；滚动输入从
  `202028..202628` 移到 `202029..202629` 时，两个重复日期行的顺序翻转。
- 翻转后 Model2 把 `202553` 的未来收益错接到 `202602`，得到 `-1`；Score 得到
  `+1`，现有 fail-closed guard 正确拒绝执行。
- 全历史 benchmark 批次因当时行数和排序分区偶然保持正确顺序，不能证明 live
  滚动窗口安全。
- 无写入内存验证中，按 `date, week_id` 的 stable 排序对完整历史 174 个算法输出点和
  2026-07-25 窗口 182 个输出点逐字段均为零差异；2026-08-01 可稳定产出
  `week_id=202629`、方向 `-1`、confidence `0.32`。

## 方案选择

采用 Model2 工程帧的确定性二级排序：先按既有 `model_date`，再按原始时间身份
`week_id`，并指定稳定排序算法。

不采用以下方案：

1. 只扩大 live 输入历史窗口：当前完整输入恰好可运行，但根因仍是非稳定排序，未来
   增加行数后仍可能复发。
2. 修改 `week_id` 到日期的原始映射：会改变 source 的分段日期语义，影响范围大于
   排序消歧。
3. 立即迁移 Blackbox V2：符合长期替代路径，但为一处已精确定位且已有零漂移验证的
   缺陷引入新身份、注册和展示迁移，不符合本次小步修正目标。

## 变更设计

### 1. 最小行为变更

在 `schemes/weekly_10y_d_overlay_0529/core/d_overlay.py` 的
`load_engineered_frame()` 中，将 Model2 进入 `create_label()` 前的排序从单键
`date` 改为 `date, week_id`，并使用 `kind="stable"`。

`model_date` 仍是第一排序键，因此所有既有 Model2 segment cutoff 语义不变；仅当
多个 week 映射到同一原始模型日期时，使用周身份恢复输入已经保证的时间顺序。Score
路径、`build_base()` 的 mismatch guard、原始归档文件和日期映射函数均不修改。

### 2. 回归测试

在专用 D-overlay 测试模块中固定两个行为：

- 以跨年四周 `202552/202553/202601/202602` 构造最小输入，验证 Model2 label 的
  顺序与 Score 时间顺序一致，`202553` 的标签为 `+1`；旧实现必须在该测试上失败。
- 使用受控窗口边界构造 `202028..202628` 和 `202029..202629`，验证两者对共同的
  跨年周得到同一 Model2 标签顺序，避免未来滚动一周再次复发。

测试只验证工程帧和标签顺序，不执行模型训练；完整算法数值等价由后续 no-persist
验证承担。

### 3. 验收与阻断条件

开发分支必须依次满足：

1. 新回归测试先在旧实现上以预期标签错误失败；
2. 最小排序修改后新测试通过；
3. 现有相关单元测试和全仓静态边界检查通过；
4. 对历史完整输入和 2026-07-25 live 输入逐字段比较，方向、confidence、Score、
   Model2、overlay、label 和 future return 均为零差异；
5. 对 2026-08-01 以正式 `scheduler.scheme_runner` 执行 no-write 复现，必须生成一条
   `feature_date=2026-07-31`、`target_date=2026-08-07` 的结果，且不再触发
   Score/Model2 mismatch；
6. `harness onboard ... --stage all` 或等价的 Native no-persist Gate 若被现有输入
   vintage 漂移阻断，必须保留失败证据并停止；不得改 benchmark、旧信号或输入快照来
   贴合。

只有这些开发验证完成且用户另行授权后，才能创建新 Native 精确版本、更新相关
production policy、写入 2026-08-01 信号或通过 launchd 重启/重新加载。

## 发布、回滚与非目标

本次开发提交不操作 MySQL、`t_scheme_predictions`、registry、installed plist、
`launchctl` 或服务进程。8 月 1 日缺失信号继续保持缺失，直到候选版本完成验证并获得
专项写库授权。

若后续激活验证失败，保留当前 active Native 版本和 failed run `1910`，不删除审计记录。
若已激活的新版本需要回滚，必须恢复完整的旧版本 bundle 与其精确 policy，不得单独
回滚一行排序代码或直接篡改数据库版本。

本设计不处理两个独立问题：现有 DB 输入 vintage 与 benchmark 的 CompareGate 漂移，
以及 launchd 内存 frozen admission 与 installed plist 的部署漂移。两者须各自形成
独立修正设计和验收证据。
