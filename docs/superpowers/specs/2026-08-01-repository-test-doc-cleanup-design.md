# 仓库测试与中间文档清理设计

**文档状态**：`APPROVED`
**确认日期**：2026-08-01
**适用分支**：`codex/audit-bugfixes-20260613`

## 1. 目标

本轮只完成三个闭环：

1. 从工作树删除已经实施完成的计划、设计过程稿和对应索引，历史仍由 Git 保存；
2. 删除能够证明已失去独立职责或已被通用契约完整覆盖的测试，不按文件年代批量删除；
3. 将 pytest 作为持久、显式的测试依赖，并把全量测试入口统一为 pytest。

本轮不合并或推送 `master`，不重载 launchd，不访问或修改生产数据库，也不删除仍有
生产代码职责的测试。

## 2. 现状诊断

仓库当前有 208 个 `test_*.py`，合计约 15.3 万行。196 个模块使用
`unittest.TestCase`，12 个模块只包含 pytest 顶层测试函数；后者共有 242 个测试。

现有全量命令是：

```bash
python -m unittest discover -s tests
```

该命令会导入 pytest 模块，却不会收集其中的 242 个顶层测试函数。因此过去通过临时目录
安装 pytest 只能避免 import 失败，不能让这 242 个测试真正执行。测试依赖缺失和错误的
全量入口必须一起修复。

`docs/superpowers/` 当前保存的是已经冻结的设计与实施过程记录。其 README 已明确说明：
被当前架构与 SOP 吸收的记录应从工作树删除并通过 Git 历史追溯。本轮按用户确认，将已完成
内容全部视为中间产物，不再保留工作树副本。

## 3. 采用方案

### 3.1 持久测试依赖与唯一全量入口

在 `pyproject.toml` 增加独立 `test` extra，固定 `pytest==9.1.1`。增加
`requirements-test.txt` 作为现有 `bond_factor_lab_service` 环境上的测试工具安装入口，
它引用项目的 test extra，不把 pytest 写入服务运行依赖 `requirements-service.txt`。

一次性执行：

```bash
conda run -n bond_factor_lab_service \
  python -m pip install -e '.[test]'
```

安装后，唯一全量测试入口为：

```bash
conda run -n bond_factor_lab_service python -m pytest
```

pytest 会同时收集原有 `unittest.TestCase` 与 pytest 顶层函数。新增 pytest 配置只声明
`tests` 为测试目录，不隐藏 warning，不排除慢测试，不用 marker 绕过失败。

首次 pytest 全量运行若暴露此前未执行的失败，必须诊断真实原因；不得为了变绿而直接删除
失败测试。

### 3.2 中间文档闭环删除

实施计划完成并用于执行后，删除整个 `docs/superpowers/` 工作树目录，包括：

- 既有 `plans/*.md`；
- 既有 `specs/*.md`；
- plans/specs 索引和顶层 README；
- 本设计与随后生成的实施计划本身。

删除前必须证明 `docs/superpowers/` 之外没有当前文档依赖这些文件。若存在引用，当前规范已
包含同等结论时删除链接；若结论尚未被当前规范吸收，则先把最小必要结论迁入对应
`docs/architecture/`、`docs/sop/` 或 `docs/CURRENT_STATUS.md`，不得保留整份过程稿。

以下内容不属于本轮文档删除范围：

- `docs/architecture/` 当前契约；
- `docs/sop/` 当前操作规范；
- `docs/blackbox_v2/records/` 与 `docs/records/` 生产证据、审计记录；
- `source_evidence/` 原始算法证据；
- `docs/CURRENT_STATUS.md` 动态状态。

### 3.3 测试清理判定

测试只有满足以下至少一条且存在替代守护时才能删除：

1. 被测生产模块已经删除；
2. 测试只守护一次性迁移、onboarding 或旧文档过渡文案，操作已经完成；
3. 相同身份、配置哈希或调度契约已经由通用 discovery/admission/scheduler Gate 覆盖；
4. 测试与另一个测试覆盖相同公开行为，删除后不会减少边界、异常或副作用验证。

第一批确定删除：

- `tests/test_docs_1y_target_visibility.py`：只防止已经完成的 1Y 文档过渡文案回退；
- `tests/test_cgb_causal_wk_1y_onboarding.py`：exact version 已由通用 Blackbox admission
  覆盖，config 变化会进入 version hash；
- `tests/test_cgb_causal_wk_3y_onboarding.py`：同上；
- `tests/test_wavg_gapflip_v5_onboarding.py`：五个 exact version、调度挂载和 admission
  已由通用测试覆盖。

首次 pytest 全量运行后，可以把新发现的冗余测试加入同一批，但每个新增删除项必须在实施
计划中写明替代测试。没有替代守护的失败测试必须修复或保留。

以下测试明确保留到对应生产代码闭环退役：

- 日度 ledger coordinator、gray runner 与常驻 APScheduler 的剩余职责；
- Actuals 一次性入口与日/周/月 updater；
- direct authority、candidate runtime 与 attestation；
- 数据库 migration、写库边界和 recovery；
- Native 源算法保真、Blackbox 契约和输入截止隔离；
- 仍保留的 destructive/admin 脚本。

特别地，不能只删除 destructive/admin 脚本的测试而保留未测试脚本。此类对象必须在后续
代码清理批次按“脚本 + 专属测试 + 文档引用”闭环删除。

## 4. 验收

完成状态必须同时满足：

1. `bond_factor_lab_service` 可直接 import pytest，不再创建临时依赖目录；
2. `python -m pytest --collect-only` 的收集结果包含此前遗漏的 242 个顶层测试；
3. 删除前后全量 pytest 均通过，删除造成的数量变化与删除清单一致；
4. `docs/superpowers/` 不再存在，当前文档不存在失效链接；
5. 核心生产测试、Actuals plist、scheduler direct authority、迁移、方案和数据库均未被改动；
6. 工作区干净，变更只保留在当前开发分支，不推送远程。

## 5. 后续边界

本轮不会为了减少测试数量而保留已退役生产代码。下一阶段应继续按模块闭环删除旧日度
ledger coordinator、gray runner 或其它确认退役职责；生产模块删除时，再同步删除其专属
测试。这样测试规模随代码规模自然下降，而不是先失去回归保护。
