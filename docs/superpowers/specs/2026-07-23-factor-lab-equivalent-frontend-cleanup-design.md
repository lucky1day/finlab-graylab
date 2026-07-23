# Bond Factor Lab 前端完全等价冗余清理设计

**日期**：2026-07-23

**状态**：设计已获用户确认，待用户复核本文档后进入实施计划

**范围**：`frontend/index.html`、`frontend/aifin-shell.js`、`frontend/aifin-shell.css` 及其前端测试

**基线提交**：`448da2fc9e3b9198909526953293aa5ab34d3031`

**实施分支**：`codex/factor-lab-equivalent-cleanup`

## 1. 决策摘要

本次采用“行为与视觉完全等价清理”，不追求最大删减：

1. 保留 AIFin/QuantFlow Logo、单项导航、数据状态条、favicon 和两个 SVG 资产。
2. 保留现有 HTML DOM 结构、可见文字、元素 ID、Class、静态资源路径和响应式布局。
3. 保留 dashboard、legacy fallback、LKG/stale、重试、灰度边界、指标计算、交互和 iframe 性能探针合同。
4. 只删除经静态引用审计和运行时测试同时证明不会执行的函数、变量、分支和 CSS。
5. 任何仍被生产分支、错误分支、空数据分支、测试钩子或部署回滚路径引用的代码，本轮均不删除。
6. 实施前先写会失败的结构测试，实施后运行 DOM、截图、请求、交互、direct URL 和跨域 iframe 等价验收。
7. 任一等价门槛不通过即停止，不提交候选发布，不改现网。

## 2. 背景

当前前端从 panda_quantflow 页面提取而来，已经精简为单一灰度因子实验室视图，但仍保留部分历史实现：

- 没有调用者的旧加载包装函数；
- 没有 DOM 或 JS 生成路径的旧组件 CSS；
- 单视图条件下不会执行的页面切换扫描动画；
- 只服务于零调用 mock 构造器的变量和辅助函数。

这些内容增加阅读和维护成本，但不是当前公网刷新耗时的主要来源。清理的首要目标是缩小误维护面，而不是用高风险重写换取有限字节收益。

基线文件规模：

| 文件 | 行数 | 原始字节 |
|---|---:|---:|
| `frontend/index.html` | 215 | 11,032 |
| `frontend/aifin-shell.js` | 2,996 | 124,028 |
| `frontend/aifin-shell.css` | 1,640 | 31,464 |

预计安全删除约 400–460 行。最终数字只作为审计结果记录，不是验收目标；不得为了达到行数而扩大范围。

## 3. 硬性不变量

### 3.1 页面不变量

以下内容必须保持：

- Logo、导航栏、数据状态条及其位置；
- 因子实验室 hero、摘要、口径与月份筛选；
- 任务格子、方案排行、月度表、趋势图和每日明细抽屉；
- 当前可见文字、列名、状态标签和空态语义；
- 桌面、980px、620px 和移动端响应式布局；
- 键盘可访问性、`aria-*` 属性和 reduced-motion 处理；
- `aifin-shell.css`、`aifin-shell.js` 和 SVG 的公开 URL。

本次不重命名 AIFin 相关文件，也不改变 HTML 中 CSS/JS 版本参数。将来发布时如需更新缓存 token，只能作为独立发布步骤处理。

### 3.2 数据与功能不变量

以下链路禁止修改：

- `/bond-factor-lab` 公网 base path 和 API URL 拼接；
- `GET /api/factor-lab/dashboard` 的严格解码、字段校验和 fail-closed 行为；
- 首次 dashboard 404 或明确 501 时的 legacy capability fallback；
- capability 锁定后禁止从 dashboard 漂移回 legacy；
- refresh generation、AbortController、竞态隔离和原子 DOM 提交；
- LKG、stale、退避重试、visibility 恢复和错误状态；
- `window.__factorLabReady` 及其 ready 字段；
- active Registry composite ID、`target_tenor + task_type` 分格；
- `predict_date`、`feature_date`、`target_date` 语义；
- gray_live、scheduled_live 和 backtest 月份边界；
- “平”计入 samples、但不进入准确率分母的指标语义；
- 排序、筛选、分页、趋势开关、抽屉和 Escape 关闭行为；
- direct URL 与跨域 iframe 中的加载完成判定。

### 3.3 部署不变量

本次不修改：

- FastAPI 路由与后端代码；
- Nginx 精确公开白名单；
- launchd、SSH 隧道和 scheduler；
- 静态资源 gzip 与缓存策略；
- 数据库和任何写库路径；
- `master` 或远程分支。

## 4. 删除允许清单

删除必须按符号或完整选择器块进行，禁止按 `factor-*`、`status-*` 等前缀批量删除。

### 4.1 JavaScript

在基线提交上，第一批允许删除：

1. 零引用变量：
   - `factorStatusLabels`；
   - 只被零调用 mock 构造器使用、且没有其它引用的名称池。
2. 零调用 mock 构造链：
   - `makeMonthRows`；
   - `makeMockDetailRowsByMonth`；
   - `createTaskSchemes`。
3. 已由事务式 dashboard/legacy candidate 加载器替代、且只有定义没有调用的包装函数：
   - `fetchLiveFactorLabTasks`；
   - `loadBacktestFactorLabData`；
   - `loadBacktestFactorLabDataSilent`。
4. 当前所有 route 都映射到同一 `factor-lab` view 时严格不可达的 scanline 动画实现。路由、base path、`postMessage` 和 `data-active-view` 合同继续保留。

删除前必须由新增测试证明目标符号没有生产调用者。若实施时发现任一新引用，立即从允许清单移除。

### 4.2 CSS

第一批允许删除仅限没有 HTML 节点、没有 JS 生成路径、没有 Class 写入路径的完整规则：

- `.eyebrow`、`.module-view .eyebrow`；
- `.factor-stack` 全组；
- `.factor-matrix`，但不是 `.factor-matrix-panel`；
- `.factor-filter-group.is-hidden`；
- `.factor-refresh-btn`；
- `.factor-accuracy-panel`，但不是 `.factor-accuracy-meta`；
- `.factor-scheme-badge`；
- 重复且已被同一选择器前一规则覆盖的 `.factor-sample-badge` 声明；
- `.factor-live-since-badge`，但不是 `.factor-live-divider`；
- `.factor-accuracy-wrap/table/score/value/track` 旧组件全组；
- `.factor-status-pill` 及其变体，但不是 `.status-strip`；
- `.route-scanline` 及只由它使用的 keyframes；
- 删除上述完整规则后确认零引用的 CSS 变量。

共享选择器中只允许删除已证明无效的 selector token，不得删除整个共享声明块。

### 4.3 HTML 与资产

本轮默认不删除或重排 HTML 节点，不删除 SVG 资产。

只有在实现中发现完全不可见、无 JS 消费且不属于可访问性合同的空白节点时，才允许先补充失败测试并单独提交设计修订；未经修订不得扩大 HTML 删除范围。

## 5. 明确禁止删除

即使看起来像历史遗留，以下内容本轮保留：

- `factorDailyBaseRows`、`factorWeeklyBaseRows`、`factorDailyRows`、`factorWeeklyRows`，因为仍有错误、mock 或空数据分支引用；
- `factorLabDataMode === "mock"` 分支；
- 每日抽屉的无真实行 fallback；
- `publicBasePath`、`apiUrl`、`normalizeRoute`、`routeUrl`；
- `setActiveRoute`、popstate、`aifin:navigate` message 监听；
- `factorLabRuntimeState` 与全部 refresh/stale/LKG 状态机；
- dashboard v1 decoder 和 view model；
- active legacy builder、merge 与回滚链路；
- 指标重算、灰度边界和同月 backtest/live 合并；
- `__factorLabTestHooks`；
- 状态条、实盘分隔、趋势图、月表、排行、任务格和抽屉样式；
- 顶栏、品牌、导航及其响应式样式。

“当前正常公网路径没有触发”不能单独作为删除依据。错误恢复和回滚功能也属于现有功能。

## 6. TDD 实施策略

### 6.1 RED

先在 `tests/test_frontend_factor_lab.py` 增加结构合同测试，至少覆盖：

- 允许清单中的死函数和变量不应存在；
- 允许清单中的死 CSS 选择器不应存在；
- Logo、导航、状态条、关键 DOM ID、资源引用必须存在；
- 禁止删除清单中的关键符号和选择器必须存在；
- HTML 可见文字和关键控件顺序必须保持。

在未修改生产文件前运行新增测试，必须因冗余仍存在而失败；失败原因必须准确指向待删除目标。

### 6.2 GREEN

按小批次实施：

1. JavaScript 零调用符号；
2. CSS 零生成路径规则；
3. 不可达 scanline 动画；
4. 共享 selector 和零引用变量收尾。

每批只做一个类别，运行新增测试和既有前端测试后再进入下一批。不得同时整理命名、改格式、拆文件或改变可见内容。

### 6.3 REFACTOR

本任务的 refactor 仅允许：

- 删除因目标块消失而产生的空注释或连续空行；
- 修复共享 selector 末尾逗号；
- 删除经再次引用扫描确认无使用者的局部常量。

不允许重新组织仍在运行的函数顺序，也不允许把 IIFE 改成模块。

## 7. 等价验收合同

### 7.1 静态与语法

- `node --check frontend/aifin-shell.js` 通过；
- `git diff --check` 通过；
- HTML 关键 DOM 合同测试通过；
- CSS 不使用外部字体或新资源；
- 修改文件仅限设计、计划、前端三文件和对应测试。

### 7.2 自动化功能测试

至少运行：

```bash
python -m pytest -q tests/test_frontend_factor_lab.py
python -m pytest -q \
  tests/test_frontend_static_cache.py \
  tests/test_public_access_config.py \
  tests/test_factor_lab_dashboard_api.py \
  tests/test_factor_lab_performance_tools.py
```

必须覆盖：

- fresh dashboard 单请求；
- dashboard/legacy capability fallback；
- 首次错误空态和刷新失败 LKG/stale；
- generation 竞态与 AbortController；
- 方案切换、排序、筛选、分页、趋势和抽屉；
- 日/周/月、平方向、待验证 actual 和灰度边界；
- direct 与 OOPIF ready 合同。

### 7.3 DOM 与可见文字等价

使用同一固定 dashboard fixture、冻结时间和相同浏览器版本，在修改前后分别保存：

- 标准化 DOM；
- 关键节点的 `textContent`、Class、`aria-*` 和控件值；
- 关键元素 computed style 摘要；
- dashboard、legacy 和其它网络请求序列。

标准化时只允许忽略浏览器生成的内部属性；业务 DOM、文本、Class 和控件状态必须完全一致。

### 7.4 像素等价

在相同 Edge 版本、相同 DPR、相同固定 fixture 和禁用动画条件下，对以下 viewport 生成修改前后截图：

- 1440×1000；
- 980×900；
- 620×900；
- 375×812。

覆盖至少：

- fresh 初始页面；
- stale/LKG 状态；
- error 空态；
- 已选择排行项；
- 趋势开关状态；
- 每日明细抽屉打开状态。

截图必须像素一致。若操作系统字体栅格造成不可重复噪声，必须先证明同一基线连续两次也产生同样差异，再以基线噪声上限作为阈值；不得直接放宽业务区域差异。

### 7.5 请求与性能等价

修改前后都运行相同的本地 direct 和跨域 OOPIF 探针：

- 每次成功加载只能有一个 dashboard 请求；
- legacy 请求数、stale、console error、page error 均为 0；
- ready 中 scheme/live/backtest 数必须一致；
- JS/CSS 原始字节不得增加；
- 20 次 canary 的 P95 必须小于 1000ms，且不得出现相对基线超过 10% 的可重复回退。

20 次只用于本地候选回归，不替代正式生产 200 次验收。生产发布须另行授权，并在 rollout 后重新执行 direct 和合成跨域 iframe 200 次验收。

## 8. 人工验收矩阵

自动化通过后，独立审查者按以下矩阵检查：

| 场景 | 验收点 |
|---|---|
| 首次加载 | Logo、导航、Loading 状态、页面无闪烁假数据 |
| fresh | 任务数、方案数、默认选中项与基线一致 |
| stale/LKG | 旧数据保留，状态提示与重试一致 |
| error/空数据 | 不展示伪造方案，空态文字一致 |
| 月份/来源筛选 | 选项、范围、排行和详情同步 |
| 排序/分页 | 顺序、方向、页码和选中项一致 |
| 趋势图 | 指标开关、轴、实盘分隔线一致 |
| 明细抽屉 | 打开、滚动、关闭、Escape 和 aria 状态一致 |
| 响应式 | 顶栏、导航、表格横向滚动和抽屉位置一致 |
| iframe | 子 frame ready、请求归属和页面显示一致 |

## 9. Subagent-driven 审查流程

实施任务由独立 implementer 子代理执行 TDD，并在每个任务后依次进行：

1. 规格符合性审查：只判断是否严格遵守允许/禁止清单和等价合同；
2. 代码质量审查：检查误删、残余引用、测试质量和可维护性；
3. 最终整体审查：复核全部 diff、自动化证据、DOM/截图/请求/性能报告。

任何 Critical 或 Important 问题必须修复并重新审查。实现者自审不能替代独立审查。

## 10. 发布与回滚

完成分支验收不等于授权生产发布：

1. 候选提交保持在 `codex/factor-lab-equivalent-cleanup`；
2. 未经用户再次确认，不合并或覆盖 `master`，不 push；
3. 未经生产发布授权，不修改现网静态文件或 Nginx；
4. 发布前记录现网 commit、静态文件哈希和回滚路径；
5. canary 任一 DOM、截图、请求或性能门槛失败，立即恢复旧前端文件；
6. 旧版本回滚不停止 SSH 隧道，不影响后端和 scheduler。

## 11. 完成定义

只有同时满足以下条件，才可称为“清理完成且不影响现有功能”：

- 删除内容全部属于本文允许清单；
- 禁止删除清单无变化；
- 相关自动化测试全部通过；
- 固定 fixture DOM 和可见文字完全一致；
- 多 viewport、多状态截图满足像素等价；
- direct/OOPIF 请求序列和 ready 数据一致；
- 本地 canary 无错误且性能不回退；
- 规格审查、代码质量审查和最终审查均无未解决的重要问题；
- 工作树无任务外修改，`outputs/` 未进入提交；
- 生产仍保持原版本，直到用户另行授权发布。
