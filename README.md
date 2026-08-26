# Bond Factor Lab 实盘测试系统

国债因子实验室实盘测试平台。Mac3 承载生产，ECS 作为独立灰度实验室；前端由 FastAPI serve，并嵌入 panda_quantflow 的 AIFin Lab Shell。

## 当前政策

- 后续新增算法、新方案 ID、新目标、新任务和替代版本只允许 Blackbox V2。
- Native V1 只维护版本化政策清单中的既有身份。
- Blackbox V2 固定只走“两文件 Intake → 一次完整持久化回测 → activate”；不进入 Native `onboard`，不额外运行 Static/Compare/冒烟/shadow。
- 技术验证不自动授予生产权限；生产运行必须逐方案完成准备核验和专项授权。
- 方案数量、Registry 状态和灰度结果按[当前状态](docs/CURRENT_STATUS.md)列出的权威来源现场读取；待验证项查看其中链接的推进计划。

## 文档入口

- [文档中心](docs/README.md)：按角色和文档域导航。
- [方案入库统一入口](docs/onboarding/README.md)：选择 Blackbox V2 或 Native V1 维护流程。
- [SOP 索引](docs/sop/README.md)：上游交付、平台入库和存量维护操作手册。
- [当前状态](docs/CURRENT_STATUS.md)：动态运行事实的唯一文档来源。
- [代码架构](docs/architecture/CODE_ARCHITECTURE.md)：系统分层、依赖、输入和写库边界。
- [灰度实验室说明手册](docs/product/GRAY_LAB_USER_MANUAL.md)：外部客户和业务读者入口。

## 运行环境

- 后端与调度：`bond_factor_lab_service`
- Native V1 算法：`forecast_env`
- Blackbox V2：由 `blackbox-v2-v1` Runtime Profile 唯一指定，不从文档或环境名称猜测解释器版本

常用只读验证：

```bash
curl -sS http://127.0.0.1:8100/api/health
curl -sS http://127.0.0.1:8100/api/factor-lab/dashboard
```

写库、activation、持久化回测和 live 命令必须按对应 SOP 获取授权，不以 README 示例代替操作门禁。

## 部署与运行产物

immutable release、launchd/systemd、Nginx、迁移和现场核验统一按[部署运行手册](deploy/README.md)
执行。运行期输入、快照、日志和报告进入 Git 忽略目录，不作为算法交付或生产配置提交。
