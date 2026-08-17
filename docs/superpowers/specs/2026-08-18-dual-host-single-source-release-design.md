# Mac3 / 阿里云 ECS 单一源码发布设计

**日期：** 2026-08-18

**当前集成分支：** `codex/aliyun-db-clone-20260816`

**冻结备份提交：** `master@2b62a2e9ae7c661f4a7f1741b5ff6c351819a4ad`

**适用阶段：** Mac3 继续承载生产，阿里云 ECS 作为独立灰度实验室

## 1. 决策摘要

Mac3 和阿里云 ECS 不维护两套源码分支，也不在服务器上分别修改代码。两台机器始终接收由同一个精确 Git 提交生成的同一份源码 release；环境差异只由一个很小的部署范围适配器、一个声明式方案矩阵和各自主机的服务配置表达。

当前阶段的分支职责如下：

- `master` 固定在 `2b62a2e9ae7c661f4a7f1741b5ff6c351819a4ad`，作为本阶段开始前的可追溯备份点，不继续承接日常提交。
- `codex/aliyun-db-clone-20260816` 是唯一活动集成分支。后续迁移、灰度修复和普通代码优化只在该分支上形成提交。
- Mac3 与 ECS 不是两个 Git 分支。发布时从集成分支的某个精确提交只构建一份 archive，先后部署到两台机器。
- 两台机器的数据库、Registry、调度启用状态、`current` 软链接和回滚动作继续相互独立。相同源码不等于同时激活。

本设计只解决“同一源码如何被两个运行环境安全复用”。它不改变算法、数据库 schema、Harness 入库门禁、Registry 生命周期或当前灰度策略。

## 2. 目标与验收口径

### 2.1 目标

1. 一次代码修改只维护一份源码，不需要向 Mac3 分支和 ECS 分支重复 cherry-pick。
2. 一个精确提交只生成一份 release，两个主机收到的源码字节和 release SHA-256 一致。
3. Mac3 可发现当前 65 个方案；ECS 只发现 C56 范围内的 56 个方案，明确排除 9 个 Darwin-only Native。
4. 环境范围不再通过修改 `config.yaml` 的 `status` 表达，避免同一算法在两个主机产生不同 `config_hash` 和 `scheme_version`。
5. 缺少生产目标、目标与控制面不匹配、矩阵不完整或出现未知目标时，在调度执行和写库前失败。
6. 生产 Python 逻辑保持小于等于 120 行净新增；若实现需要扩散到 repository、executor、backend API、actuals 核心或 Harness 业务逻辑，停止实现并重新评审设计。

### 2.2 完成标准

- 未设置部署目标的开发/Harness 发现仍精确得到全部 65 个方案。
- `mac3-production` 目标发现 65 个方案。
- `aliyun-gray` 目标发现 56 个方案，且排除集合精确等于本设计列出的 9 个方案。
- 9 个方案的 canonical `config.yaml` 恢复为 `status: active`，不再承载 ECS 环境差异。
- Matrix 必须与 `schemes/*/config.yaml` 一一覆盖；少一项、多一项、重复目标或未知目标均失败。
- launchd 只能与 `mac3-production` 配对，systemd 只能与 `aliyun-gray` 配对；缺失或错配在任何数据库或算法副作用前失败。
- ECS 的 9 个 composite Registry 行继续保持 `paused`，Mac3 的 Registry 不因 ECS 范围而改变。
- 普通代码优化不修改矩阵时，可从同一提交构建一个 archive，并在两台机器上校验为同一摘要。

## 3. 分支与发布模型

### 3.1 当前分支策略

本阶段只有一条活动开发线：

```text
master@2b62a2e (冻结备份)
          \
           codex/aliyun-db-clone-20260816 -> 后续提交
```

不创建 `mac3`、`ecs`、`production-mac` 或 `gray-ecs` 等长期环境分支。主机状态不通过 Git 分支表达，避免同一修复在两条长期分支间漂移、遗漏或产生不同合并顺序。

需要并行开发时，可以从当前集成分支创建短期 `codex/<task>` 分支，验证后再合回该集成分支；它们是任务分支，不是环境分支。未经用户另行授权，不移动或推送 `master`。

### 3.2 单一 release

每次候选发布只接受一个精确提交：

1. 工作树干净且测试通过。
2. 使用 `git archive` 从该提交生成一次源码包。
3. 记录提交 SHA、archive 文件名和 SHA-256。
4. 同一 archive 先暂存到 ECS；观察通过后，再把完全相同的 archive 暂存到 Mac3。
5. 每台主机独立解压到 `releases/<exact-commit>/`，独立切换 `current` 软链接。

ECS 继续采用 release-only 目录，不保存 `.git`，不执行 `git pull`，不允许主机本地修改源码。Mac3 后续也应迁移到相同的不可变 release 目录模型，但切换 installed launchd 路径属于独立生产操作，必须另行授权；在此之前，本设计不触碰 Mac3 现场。

### 3.3 独立晋级与回滚

源码相同，激活状态独立：

- ECS 可以先切换到候选 release 并独立观察。
- Mac3 只有在用户明确授权后才切换到已经观察过的同一 release。
- ECS 回滚只移动 ECS 的 `current`；Mac3 回滚只移动 Mac3 的 `current`。
- Registry 同步、方案 activation、数据库 migration、timer/launchd 启用都分别执行和验收，不能因源码相同而自动外推授权。

## 4. 最小部署范围适配器

### 4.1 唯一环境变量

新增环境变量：

```text
BFL_DEPLOYMENT_TARGET=mac3-production
BFL_DEPLOYMENT_TARGET=aliyun-gray
```

变量只写入受版本控制的 launchd/systemd 服务模板和对应 installed 服务环境，不写入全局 shell profile、conda 环境或开发者终端。

未设置该变量时，普通开发、单元测试和 Harness 仍发现完整方案集合。生产 one-shot 调度则必须显式设置并校验该变量，不能使用未设置时的开发默认行为。

### 4.2 声明式矩阵

新增 `deploy/scheme_deployment_matrix_v1.json`，结构固定为：

```json
{
  "schema_version": "scheme-deployment-matrix-v1",
  "targets": ["mac3-production", "aliyun-gray"],
  "schemes": {
    "example_shared_scheme": ["mac3-production", "aliyun-gray"],
    "example_mac_only_scheme": ["mac3-production"],
    "example_staged_scheme": []
  }
}
```

矩阵是部署资格声明，不是算法状态或 Registry 状态：

- 同时列出两个目标：同一算法允许在两个主机部署。
- 只列 `mac3-production`：当前只允许 Mac3 运行。
- 空数组：代码可以合入和随 release 分发，但两个生产目标都不得发现或执行。
- 每个 `schemes/*/config.yaml` 必须在矩阵中恰好出现一次；矩阵不得引用不存在的方案。

当前 9 个只允许 Mac3 的 Native 为：

```text
daily_1y_xgb_1y13_0629
daily_5y_lgbm_5y10_0629
daily_10y_lgbm_10y04_0629
weekly_avg_1y_lgbm_0529
weekly_avg_5y_lgbm_0529
weekly_avg_10y_lgbm_0529
monthly_1y_rf_top30_0629
monthly_5y_knn_top20_0629
monthly_10y_rf_top5_0629
```

其余当前 56 个方案同时允许两个目标。当前没有 ECS-only 方案。

### 4.3 单点接入

新增一个 `scheduler/deployment_scope.py` 小模块，只承担三件事：

1. 读取并严格校验矩阵。
2. 解析 `BFL_DEPLOYMENT_TARGET`。
3. 对 `scheduler.discovery` 已完整加载并校验的 `SchemeConfig` 列表做目标过滤。

`scheduler.discovery.discover_schemes()` 是唯一接入点。它先加载和验证全部 config，再在设置了合法 deployment target 时过滤结果。repository、executor、backend Registry sync、actuals updater 和调度 runner 继续使用现有 discovery，不各自增加环境判断。

生产 one-shot runner 还要在进入 discovery、数据库或算法副作用前校验控制面与目标：

| 控制面 | 唯一合法目标 |
|---|---|
| `launchd_one_shot` | `mac3-production` |
| `systemd_one_shot` | `aliyun-gray` |

executor 的 scheduled-live canonical config 校验继续重新调用 discovery，因此 ECS 上被矩阵排除的方案不会得到 canonical config，无法通过直接构造参数绕过调度范围。

### 4.4 代码量止损线

预期改动：

- `scheduler/deployment_scope.py`：约 60–90 行。
- `scheduler/discovery.py`：约 3–8 行接入。
- one-shot 目标/控制面前置校验：约 10–20 行。
- 矩阵、service 环境行和测试不计入生产 Python 逻辑。

生产 Python 净新增硬上限为 120 行。实现中若超过上限，或需要在 repository、executor、backend API、actuals 核心、Harness Gate 中分别实现过滤，说明单点边界不成立，必须停止并重新设计，不能用更多抽象掩盖扩散。

## 5. 配置、版本与 Registry 边界

### 5.1 恢复 canonical config

当前 9 个方案因 C56 候选范围曾把 `config.yaml` 的 `status` 改成 `paused`。Native 的 `config_hash` 和 `scheme_version` 基于原始 config bytes，因此该做法会让 Mac3 和 ECS 对同一算法得到不同 exact version。

实现本设计时，这 9 个 `status` 恢复为 canonical 的 `active`。矩阵负责表达“ECS 不运行”，config 只表达算法版本本身的生命周期。不得通过平台判断后再动态改写 config 或 version hash。

### 5.2 Registry 不自动推断删除

discovery 过滤只决定当前主机可以看到和执行哪些方案。它不会把 Registry 中未出现的历史行自动删除或自动降级：

- ECS 当前 9 个 composite Registry 行已经是 `paused`，部署后必须读回并保持该状态。
- Mac3 的对应 Registry 行继续由 Mac3 自身状态管理。
- 将一个已经 active 的方案从某目标移除时，必须先走该环境受控的 Registry pause/读回，再部署新矩阵；不能仅修改 JSON 后假定数据库会自动变化。
- 将方案加入某目标时，也必须分别满足该环境既有的 exact-version、Harness、activation 和 Registry 门禁。

矩阵资格、config/version 生命周期、Registry 业务可见性和 OS scheduler 触发是四个不同层次，不相互替代。

## 6. 服务模板与运行时行为

受版本控制的服务模板增加目标环境：

- Mac3 的 backend、DataBridge、daily、weekly、monthly、actuals launchd 模板使用 `mac3-production`。
- ECS 的对应 systemd service 使用 `aliyun-gray`。
- SSH tunnel 不参与方案 discovery，不增加该变量。

首次把含目标强校验的新 release 部署到某主机前，必须先只读核对 installed 服务环境。模板文件变更不等于现场安装：

- Mac3 installed plist 替换、`launchctl bootout/bootstrap/kickstart` 仍需独立授权。
- ECS systemd unit 安装、`daemon-reload`、服务重启仍需独立授权。
- 本设计及其后续代码实现都不自动启用 ECS timers。

目标缺失、值未知、矩阵 schema 错误、矩阵覆盖不完整或控制面错配时，runner 输出脱敏配置错误并以非零状态结束，不创建 run、不写 prediction、不启动算法子进程。

## 7. 日常维护流程

### 7.1 普通代码修复或优化

不涉及方案部署范围时：

1. 只在活动集成分支修改一次代码。
2. 完成与风险相称的测试和 Gate。
3. 从通过验证的精确提交构建一个 archive。
4. 将同一 archive 部署到 ECS 并观察。
5. 用户确认后，将同一 archive 部署到 Mac3。

普通修复不修改矩阵，不产生 Mac/ECS 两份补丁。

### 7.2 新方案

新方案仍只允许 Blackbox V2，并继续经过既有 Intake 和 Harness：

1. 代码首次合入时可在矩阵中设为空数组，表示随 release 分发但不在任何生产目标运行。
2. 通过 ECS 准备检查后，把 `aliyun-gray` 加入该方案目标并独立 activation。
3. 灰度观察通过、用户授权 Mac3 晋级后，再把 `mac3-production` 加入目标并独立 activation。

每次目标变化都是一个可审查的数据行变化，不复制算法目录或配置文件。

### 7.3 平台特有文件

launchd plist、systemd unit、Linux/macOS 环境锁可以存在于同一个提交中。它们是同一源码 release 内的部署物料，不是两套业务代码。主机只安装自己的平台文件，另一平台文件留在 release 中但不执行。

## 8. 测试与验收

### 8.1 单元与契约测试

至少覆盖：

1. 未设置目标时发现 65 个方案。
2. Mac3 目标发现 65 个方案。
3. ECS 目标发现 56 个方案并精确排除 9 个 ID。
4. Matrix 与 config 双向完全覆盖，目标列表无重复且只含两个合法值。
5. 9 个 canonical config 均为 `active`；Native policy 仍为 26 个，owner 映射仍为 69 个。
6. 未知目标、错误 schema、缺失方案、多余方案均 fail-closed。
7. launchd/Mac3 与 systemd/ECS 正常；缺目标和交叉错配在数据库或 executor mock 调用前失败。
8. ECS target 下直接请求被排除 scheme 的 scheduled-live canonical 校验失败。
9. Harness/onboarding 在未设置目标的测试环境仍看到完整方案集合。

随后运行相关 discovery、repository、executor、launchd/systemd、actuals、Harness 契约测试和完整测试集。不得用只验证数量的测试替代 9 个精确 ID 断言。

### 8.2 ECS 部署读回

后续取得部署授权后，ECS 必须读回：

- release commit 与 Mac3 候选 archive 摘要一致。
- `BFL_DEPLOYMENT_TARGET=aliyun-gray`，control plane 为 `systemd_one_shot`。
- unscoped 源码清单 65，ECS effective discovery 56。
- Registry 60 active composite、9 个精确 paused composite。
- 手工 one-shot 只对 56 个范围内方案创建 run；9 个排除方案执行数为 0。
- timers 继续 disabled/inactive，直到另行授权。

### 8.3 Mac3 部署读回

Mac3 仍是生产操作，只有另行授权后执行：

- release commit 和 archive SHA-256 与已观察 ECS release 一致。
- `BFL_DEPLOYMENT_TARGET=mac3-production`，control plane 为 `launchd_one_shot`。
- effective discovery 为 65。
- Registry、installed plist、loaded launchd 和业务写入保持既有生产语义。

## 9. 明确不做

- 不维护两个长期环境分支。
- 不在 ECS 保存 Git checkout 或执行 `git pull`。
- 不复制 scheme 目录，不生成 Mac/ECS 两份 config。
- 不把主机名、操作系统判断或数据库地址写进算法代码。
- 不新增数据库表或 migration。
- 不恢复 APScheduler、ledger、occurrence、epoch 或第二 Python 调度控制面。
- 不在本轮引入 GitHub Actions、artifact server、自动跨主机发布或自动晋级。
- 不因实现设计而启用 timer、切换域名、修改 Mac3 installed launchd 或重启生产服务。

## 10. 实施顺序

本设计通过书面复核后，再编写独立实施计划。计划必须按以下边界拆分：

1. 先用测试锁定矩阵契约、目标映射和 fail-closed 行为。
2. 实现小适配器并接入 discovery，核对生产 Python 净新增不超过 120 行。
3. 恢复 9 个 canonical config 状态并更新现有 C56 契约测试。
4. 更新受版本控制的 launchd/systemd 模板，但不安装、不重启、不启用 timer。
5. 运行相关测试和完整测试集，形成精确 commit。
6. 在获得独立授权后才构建单一 archive、部署 ECS 和执行现场读回。
7. Mac3 发布继续作为更晚的独立授权步骤。

该顺序确保代码设计、release 构建、ECS 激活和 Mac3 生产变更四类授权不会互相外推。
