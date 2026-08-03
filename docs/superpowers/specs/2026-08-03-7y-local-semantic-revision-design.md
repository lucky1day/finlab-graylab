# 7Y 本地因果语义修订设计

**状态：用户已于 2026-08-03 授权执行**

## 目标

将当前两套无法在合法 T+1 cutoff 下运行的 7Y Blackbox 交付，作为两个独立的本地
因果修订方案重新交付、验证并受控接入平台：

- `seven_y_current55_lgbm_001_v2`
- `seven_y_current55_lgbm_002_v2`

旧 `*_v1` 目录和字节证据保持不变，绝不原地覆盖。

## 时序语义

对日频 T+1 请求，`feature_date=T` 是 daily/weekly/monthly 输入硬截止，`target_date`
是下一交易日 `T+1`。修订版以 `feature_idx` 作为唯一的预测状态索引：

1. 模型查询使用 feature frame 的 `feature_idx` 行；其监督标签仍为
   `target_by_pred[feature_idx] = sign(yield[T+1] / yield[T])`。
2. 每月模型重训、年度特征筛选和训练 cutoff 按 `feature_date` 所在状态确定；训练样本
   只可使用其 forward label 在 T 前已经观测完成的行。
3. 翻转统计以历史 feature 行的 `target_by_pred` 标签评估，不能再把“目标索引”与
   “特征索引”混用。
4. 输入仍严格经已有 `clipped()` 截断；未来日、周、月数据不参与任何特征、训练或预测。

该版本保留现有交付的特征变换（包括其既有的可用性滞后），以避免在本次因果修复中顺带
重写特征集合。故其定位为**本地因果修订 trial**，不宣称与 `prod_screen` 的完整内部数值
保真。

## Cache 与交付边界

模型只在单个 CLI 进程的内存字典中复用。修订版删除 `--cache-dir`、pickle 读取/写入和
`.blackbox_model_cache` 默认路径；交付进程只能创建显式 `--output` 文件。

每个 v2 方案是独立的同名 `.py + .json` 两文件交付，经标准 Intake 生成
`paused + draft` config。002 继续使用长窗参数，不能因 ID 后缀变化退化为 001 参数。

## 验证与写入边界

先以专用真实 CLI 回归测试证明 T cutoff 可输出、后缀数据不影响结果、两个冷进程一致且
无磁盘 cache；随后每个 v2 方案单独完成全量 Blackbox Gate。只有 Gate 通过后，才依用户
授权执行 draft/shadow/activation、`target_date < 2026-06-01` 的历史回测持久化，以及
历史 `gray_live` 回补和 API/前端读回。定时 scheduler admission、installed plist 和
`launchctl` 始终不在本次范围内。
