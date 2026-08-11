# 隐藏缺失信号前端提示设计

## 目标

因缺失信号而产生的说明文字不再显示在因子实验室前端排行表中。用户将不再看到
“信号缺失 · &lt;原因&gt;”或其任何 failure category。

## 现状

`frontend/aifin-shell.js` 的 `renderSchemeRankingRow()` 会在
`signalStatus === "missing"` 时生成 `factor-signal-missing` 标签，并将
`signalFailureCategory` 显示在方案名称下方。该标签只有这一处渲染入口；对应样式
只有 `.factor-signal-missing` 一条规则。

前端仍会在 dashboard payload 解码阶段校验 `signal_status` 与
`signal_failure_category` 的组合。这是接口契约，不是展示提示的一部分。

## 设计

- 删除 `renderSchemeRankingRow()` 中缺失信号标签的 HTML 生成与插入。
- 删除不再被使用的 `.factor-signal-missing` CSS 规则。
- 保留 dashboard payload 的字段解码、状态值验证和 failure category 一致性校验。
- 不改后端 API、数据库、信号计算、排序、指标或其它前端状态提示。

这样，信号缺失仍能被接口契约识别，但不会产生任何面向用户的缺失信号文字或元素。

## 测试设计

扩展现有 `tests/test_frontend_ranking_contract.py` 的静态前端契约检查：

- 保持排行榜已有样本数显示断言；
- 断言 JavaScript 与 CSS 都不再包含 `factor-signal-missing`；
- 断言 JavaScript 不再包含“信号缺失”文案。

现有 dashboard API 和 gate 测试继续覆盖 `signal_status` 与
`signal_failure_category` 的解码和校验，不为本次展示变更修改它们。

## 完成标准

- 前端资源中不存在缺失信号提示的 HTML、样式选择器或中文文案；
- `signal_status` 与 `signal_failure_category` 仍在 payload 解码中被验证；
- 聚焦前端契约测试和相关 dashboard 测试通过；
- `git diff --check` 通过；
- 改动仅限本次前端展示和对应测试。

## 非目标

- 不移除、重命名或改变任何 API 字段；
- 不改变缺失信号的后端判定或错误类别；
- 不隐藏“数据不可用”等非缺失信号状态；
- 不修改生产调度、plist、数据库或算法逻辑。
