# 任务格准确率红色高亮设计

## 决策

用户已在视觉对比中选择“仅数值红色”方案：当任务格当前排序第一名的
`overall`、`upPrecision` 或 `downPrecision` 聚合准确率达到 `60%` 时，仅
显示该任务格的准确率数字为红色。

## 范围

- 保留现有 `is-accuracy-highlighted` class 与其 `>= 60`、有限数值、准确率
  指标白名单的判定逻辑。
- 移除该 class 的绿色背景和 inset 边框。
- 通过 `.factor-task-cell.is-accuracy-highlighted .factor-task-top` 使用既有
  `--negative` 主题色，使数值显示为红色。
- 不改变排序、筛选、选中、hover、键盘行为、HTML 结构、API、前端状态、
  数据库或后端接口。
- CSS 变更必须独立更新 `frontend/index.html` 中的 CSS version token，且该
  token 必须是 `frontend/aifin-shell.css` 精确 bytes 的完整 SHA-256 摘要。当前
  精确 token 的静态响应使用 `public, max-age=31536000, immutable`，因此 CSS
  内容变更会强制生成新的 URL，不能复用旧 token。未变更的 JS 保持其原有 token。

## 方案比较

1. 推荐：保留父级语义 class，使用子元素 CSS 改变数值颜色。改动最小，且
   selected/hover 继续独占任务格背景层级。
2. 在 JavaScript 内联设置颜色。会把视觉样式混入渲染逻辑，不采用。
3. 修改渲染 HTML，额外给数字添加 class。可行但没有必要；现有父级 class 和
   `.factor-task-top` 已足够精确。

## 验收

- 现有高亮条件的 DOM 测试继续通过。
- CSS 契约断言高亮规则没有 `background` 或 `box-shadow`，且准确率数字使用
  `var(--negative)`。
- 静态缓存契约从 CSS bytes 动态导出完整 SHA-256，断言 index 的 CSS token 集
  精确等于该摘要、当前真实静态根目录 URL 为 immutable、过期 CSS URL 强制
  no-cache/revalidate；JS 仍为 `20260806a`。
- 前端与静态缓存测试、`git diff --check` 通过。
