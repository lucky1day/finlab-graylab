# 五个算法独立交接包设计

## 目标

从灰度所使用的精确代码中生成一个 `bond_algorithms_5` 交接包，供同事在新的 Apple Silicon Mac
上部署。交接只重新组织现有文件，不修改算法逻辑、参数、输入方式、运行接口或编译产物。

## 精确范围

交付以下五个方案：

- `weekly_avg_1y_lgbm_0529`
- `weekly_avg_5y_lgbm_0529`
- `weekly_avg_10y_lgbm_0529`
- `cgb_a4_fundseason_5y`
- `cgb_a4_fundseason_10y`

源代码身份固定为灰度 R4 commit
`92a93713656d8534e68312f123676b5d2054d8a6`。打包不得从未提交工作区或其它近似版本取文件。

## 交付结构

```text
bond_algorithms_5/
├── requirements.txt
├── weekly_avg_1y_lgbm_0529/
│   ├── config.yaml
│   └── forecast_project/
├── weekly_avg_5y_lgbm_0529/
│   ├── config.yaml
│   └── forecast_project/
├── weekly_avg_10y_lgbm_0529/
│   ├── config.yaml
│   └── forecast_project/
├── cgb_a4_fundseason_5y/
│   ├── config.yaml
│   └── delivery/
│       ├── cgb_a4_fundseason_5y.py
│       └── cgb_a4_fundseason_5y.json
└── cgb_a4_fundseason_10y/
    ├── config.yaml
    └── delivery/
        ├── cgb_a4_fundseason_10y.py
        └── cgb_a4_fundseason_10y.json
```

输出中不出现 `source_evidence/` 路径。

## 文件来源与复制规则

三个周平均目录中的 `forecast_project/` 均完整复制自 R4 中：

```text
source_evidence/benchmark_batches/model_muti_0529/
weekly_average_0529/source_package/forecast_project/
```

三份复制必须逐字节一致，并与 R4 原始目录的确定性 tree digest 一致。重复是明确的交付要求：每个
周平均目录自身包含完整 backend，不依赖包内共享的 `forecast_project/`。

三个周平均 `config.yaml` 分别取自 R4 对应的 `schemes/<scheme_id>/config.yaml`。根目录
`requirements.txt` 取自上述原始 `forecast_project/requirements.txt`，作为五个算法共用的环境依赖。

两个 Macro 目录只复制 R4 中对应的 `config.yaml` 和 `delivery/{scheme_id}.py +
delivery/{scheme_id}.json`，不增加包装器或改写 Blackbox Contract 1.0 CLI。

## 运行形态

五个算法共用同一个 `forecast_env`。已核对的目标兼容基线为 CPython 3.13、Darwin arm64；该环境可
同时加载周平均 backend 所需依赖，并执行两个 Macro 的 `--version` 入口。

三个周平均 backend 保留当前入口，通过原生参数区分期限：

- 1Y：`--frequencies W1Y`
- 5Y：`--frequencies W5Y`
- 10Y：`--frequencies W10Y`

两个 Macro 保留当前 `predict/backtest --data-dir ...` 接口。交接包不统一这两类算法的输入方式，
也不加入数据库、调度、Registry、DataBridge 或平台部署能力。

## 排除项

交接包不得包含：

- `shared/`、`scheduler/`、`backend/`、`harness/`；
- 顶层 `source_evidence/` 目录；
- Git 元数据、`.env`、DSN、密码、token 或本机配置；
- `outputs/`、日志、缓存或运行期数据库内容；
- launchd、systemd、Registry 或 release 激活脚本；
- 任何重新编译、反编译、参数调整或算法重写。

## 失败与完整性规则

下列任一情况必须停止打包，不生成可交付结果：

- R4 中缺少任一要求的源文件；
- 三份 `forecast_project` 与原始 tree digest 不一致；
- 三份 `forecast_project` 相互不一致；
- 周平均 Mach-O 扩展不是 Darwin arm64 或不能由目标 CPython 3.13 加载；
- Macro `--version` 不能在共用环境运行；
- 输出包出现范围外文件、符号链接或敏感配置。

## 验收

打包完成后执行以下只读验证：

1. 精确枚举顶层目录和五个方案目录，确认与本设计一致。
2. 计算原始及三份复制的 tree digest，确认四者相同。
3. 在 `forecast_env` 中分别加载三个周平均 backend，并读回 `W1Y/W5Y/W10Y` CLI 支持。
4. 在同一环境运行两个 Macro 的 `--version`。
5. 检查输出中不存在排除项、交接过程新增的绝对路径配置或新增算法代码；原始
   `forecast_project` 已有的路径候选和说明文字保持字节不变。
6. 生成最终 `tar.gz`，记录文件大小和 SHA-256。

验收不连接生产数据库、不执行预测、不写业务表，也不修改任何生产服务。
