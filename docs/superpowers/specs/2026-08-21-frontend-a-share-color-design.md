# 前端 A 股红绿颜色统一设计

## 目标

只调整灰度实验室前端的百分比、方向和结果状态颜色，不改变任何指标计算、格式、排序、筛选、接口、后端、数据库、页面结构、文案、交互或趋势图系列颜色。

## 最小设计

### 百分比

保留一个公共的原始数值判断函数，返回三个语义类：

- `metric-high`：有限数值且 `value >= 60`；
- `metric-low`：有限数值且 `value < 60`；
- `metric-empty`：`null`、`undefined`、空字符串、`--` 或不可转换为有限数值的值。

任务格子最优指标、候选方案排行和逐月表现全部复用这个函数。CSS 仅在这三个组件内把 `metric-high` 映射为现有红色 `var(--negative)`、`metric-low` 映射为现有绿色 `var(--accent)`、`metric-empty` 映射为现有灰色 `var(--text-tertiary)`。删除百分比表格对 `metric-warn`/`var(--gold)` 的使用，不影响其它黄色 UI。

### 涨跌方向

增加单一方向类映射：

- `涨 -> direction-up -> var(--negative)`；
- `跌 -> direction-down -> var(--accent)`；
- `平`、空值、`--` 和未知值 `-> direction-neutral -> var(--text-tertiary)`。

预测方向和实际方向调用同一个映射。

### 结果状态

结果列继续独立表达预测正确性：

- `true -> is-correct -> var(--accent)`；
- `false -> is-wrong -> var(--negative)`；
- 其它值 `-> is-neutral`，显示现有 `?`，使用现有灰色 neutral 样式。

方向颜色变化不得影响结果列。

## 测试与验收

Node 测试直接执行浏览器脚本暴露的测试 hook，覆盖 `0`、`59.9`、`60.0`、`60.1`、`100`、空值、`--`、非法值，以及涨/跌/平/未知方向和 true/false/待验证结果。静态合同同时确认排行、逐月、任务格子复用公共函数，百分比组件不再使用黄色类，趋势图固定系列颜色不变。

修改 JS/CSS 后重新计算 `frontend/index.html` 的内容哈希查询参数。自动化测试通过后，从精确源码提交构建 immutable release，先部署 ECS 灰度实验室，再用实际浏览器检查任务格子、排行、逐月、每日明细及非目标文本/趋势图颜色。

## 非目标

不修改指标计算与展示精度、样本分子分母、排序筛选、数据合并、API/后端/数据库、页面结构、表格列、文案交互、趋势图系列色、普通文字色或生产 Mac3。
