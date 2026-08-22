# Weekly 1Y 方案 Mac3 完整入库设计

**日期：** 2026-08-23
**目标方案：** `weekly_1y_causal_v1_31_0_standalone`

## 目标

在不复制 ECS 数据库、Registry 或运行状态的前提下，让已经在 ECS 独立灰度实验室完整入库的 Blackbox V2 方案也在 Mac3 生产环境完成独立入库。Mac3 必须使用与 ECS 相同的新精确 source release，并在本机重新建立平台 Gate、方案生命周期、全量回测、灰度预测和 Dashboard 证据。

## 决策

1. 将方案部署矩阵从仅 `aliyun-gray` 改为同时允许 `mac3-production` 和 `aliyun-gray`。环境差异仍只由 `BFL_DEPLOYMENT_TARGET` 和部署矩阵表达。
2. 将 canonical 生命周期声明恢复为安全初态 `paused/draft`。生命周期字段不参与该 Blackbox 的 `scheme_version`；ECS 已激活状态必须由其精确 host overlay 保持，Mac3 则从安全初态经受控入口首次登记。
3. ECS 现场目前是“DB/Registry 已 active，但无 host overlay，依赖旧 canonical `active/active` fallback”。发布前增加一个狭窄、显式授权、insert-only 的 lifecycle bootstrap：只允许 canonical、exact version DB 和 composite Registry 全部已经 active，且 overlay 不存在时，把同一 active 状态持久化到 host overlay；任何不一致或已存在 overlay 均 fail-closed。该入口不改 DB、不激活新身份、不适用于 paused/draft 或版本迁移。
4. 使用两级不可变 release，避免候选代码、旧 project root 和 release identity 混用：A release 只加入 bootstrap 工具并保持旧 canonical `active/active`/ECS-only，先晋级 ECS 后执行 bootstrap；B release 才加入 `paused/draft` 与双环境矩阵。B 先晋级并复验 ECS，再由 Mac3 使用同一 B archive。
5. Mac3 使用 `osx-arm64` 环境清单和本机 DataBridge，重新运行该 exact version 的 Blackbox `all` Gate。不得把 ECS Harness receipt 当成 Mac3 Gate。
6. Gate 通过后按 `shadow-register → persistent backtest → source CAS → activate → gray gap fill → dashboard` 顺序执行。数据库和 Registry 全程保持环境独立。
7. 不替换 launchd plist，不改变触发时间，不手工重跑无关 Writer。激活后的方案由现有周频 one-shot 在下一自然触发时纳入。

## 发布与回滚边界

- source release 的身份由精确 Git SHA、archive SHA-256 和 manifest 共同确定；生产 release 不携带 `.git`。
- ECS 与 Mac3 的 `current` 分别用各自 expected-current 做 CAS；不允许静默覆盖现场变化。
- source CAS 失败时保持原 `current`。激活前失败时保留 paused/shadow 状态，不写 live。
- lifecycle bootstrap 只有 overlay 写入这一项副作用；失败不改 DB，重试前必须只读判断 overlay 是否已经精确落地。
- 激活或 gray gap fill 失败时，不覆盖已发布预测；按既有 compensating/insert-only 语义保留审计证据。
- `previous` 仅用于源码回滚；数据库、回测和运行状态不随 symlink 自动回滚。

## 验收标准

- 部署范围测试明确保护目标方案同时属于两个 target。
- ECS 和 Mac3 最终安装的是同一份 B archive，manifest commit 与候选精确提交一致；A release 仅作为 ECS lifecycle 迁移台阶保留审计身份。
- Mac3 的 osx 环境校验和该方案 `all` Gate 全部通过。
- Mac3 存在 exact version、active composite Registry、持久化全量回测，以及 `2026-08-22` 的 insert-only `gray_live` 预测。
- `/api/schemes`、DashboardGate 和浏览器页面均能读到该 composite 方案。
- installed/loaded launchd 漂移审计仍通过，五个 Writer 没有被替换或改时。
