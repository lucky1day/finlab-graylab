# DataBridge 运行期目录

`current/` 保存 producer 发布的完整输入 generation，不进入 Git。生产路径与环境加载方式见[部署运行手册](../../deploy/README.md)；五文件、ready gate、版本兼容和样例的权威说明见[DataBridge 契约](../../docs/blackbox_v2/data_bridge_v1/README.md)。

算法只使用平台给出的只读 `--data-dir`，不得硬编码本目录。完整数据、staging、previous、Snapshot 和刷新产物均属于运行期资产，不复制到文档或提交到 Git。
