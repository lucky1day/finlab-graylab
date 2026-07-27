# Bond Factor Lab 实盘测试系统

国债因子实验室实盘测试平台，在 Mac Studio 上统一运行预测、回测、调度、写库和前端展示。前端由 FastAPI serve，并嵌入 panda_quantflow 的 AIFin Lab Shell。

## 当前政策

- 后续新增算法、新方案 ID、新目标、新任务和替代版本只允许 Blackbox V2。
- Native V1 只维护版本化政策清单中的既有身份。
- Blackbox V2 自动 Gate 不自动授予生产权限；生产运行必须逐方案完成准备核验和专项授权。
- 方案数量、Registry 状态、灰度结果和待验证项统一查看[当前状态](docs/CURRENT_STATUS.md)。

## 文档入口

- [文档中心](docs/README.md)：按角色和文档域导航。
- [方案入库统一入口](docs/onboarding/README.md)：选择 Blackbox V2 或 Native V1 维护流程。
- [SOP 索引](docs/sop/README.md)：上游交付、平台入库和存量维护操作手册。
- [当前状态](docs/CURRENT_STATUS.md)：动态运行事实的唯一文档来源。
- [架构与契约](docs/architecture/README.md)：系统、代码、Harness、日期和共享契约。
- [灰度实验室说明手册](docs/product/GRAY_LAB_USER_MANUAL.md)：外部客户和业务读者入口。

## 运行环境

- 后端与调度：`bond_factor_lab_service`
- Native V1 算法：`forecast_env`
- Blackbox V2：由 `blackbox-v2-v1` Runtime Profile 唯一指定，不从文档或环境名称猜测解释器版本

常用只读验证：

```bash
curl -sS http://127.0.0.1:8100/api/health
curl -sS http://127.0.0.1:8100/api/schemes
curl -sS http://127.0.0.1:8100/api/backtests/factor-lab
```

写库、activation、持久化回测和 live 命令必须按对应 SOP 获取授权，不以 README 示例代替操作门禁。

## Canonical migration runner

迁移的唯一行为实现是 `migrations.runner`；它只接受 caller-supplied `Engine`。
`scripts/apply_migrations.py` 是唯一受控 operator CLI：写入 schema 时必须提供预期
database name 与 server UUID，先以只读 inspect 或受控只读 identity query 取得这两个值，
再按[部署运行手册](deploy/README.md)执行。不得用 `mysql` 直跑 migration SQL，也不得把
隔离 MySQL 测试当作生产 migration 已应用的证据。当前 CLI 的 apply/no-op 结果还不是
durable signed operator report。

## 目录说明

```text
bond-factor-lab/
├── shared/             # 公共输入、日历、模型和数据服务
├── schemes/            # Native V1 存量方案与 Blackbox V2 新方案
│   ├── {native_id}/    # config + predict + core
│   └── {blackbox_id}/  # config + delivery 两文件
├── scheduler/          # discovery、executor、repository、actuals updater
├── backend/            # FastAPI API 与静态前端 serve
├── backtests/          # 历史回测 runner
├── harness/            # Gate、授权和审计证据
├── frontend/           # 原生 HTML/CSS/JS 因子实验室页面
├── migrations/         # 数据库迁移
├── deploy/             # launchd、nginx 和部署配置
├── tests/              # 单元与集成测试
└── docs/               # 唯一文档入口见 docs/README.md
```

运行期输入、快照和报告分别进入 `backtest_artifacts/`、`data/` 和 `reports/` 的忽略目录，不作为算法交付或生产配置提交。
