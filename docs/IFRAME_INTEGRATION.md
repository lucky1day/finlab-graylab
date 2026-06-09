# panda_quantflow iframe 集成说明

**更新日期**: 2026-06-07

## 当前可嵌入地址

当前 Bond Factor Lab 由 launchd 常驻服务提供:

```text
http://127.0.0.1:8100/
```

本机 HTTP 响应未设置 `X-Frame-Options` 或阻止 iframe 的 CSP 头，可作为 iframe 内容页加载。

当前已验证 `GET /api/health` 返回 `{"status":"ok"}`，`GET /api/targets` 返回 `3Y/5Y/7Y/10Y` 四个目标。`GET /api/backtests/factor-lab` 返回 t1/t5 日度回测数据。周度 10Y 回测（原 `weekly_10y_d_overlay`）已随周度方案退役，当前无周度回测数据返回。

## 外层页面嵌入片段

如果 panda_quantflow 与 Bond Factor Lab 运行在同一台 Mac 上，外层 AIFin Lab Shell 可使用:

```html
<iframe
  src="http://127.0.0.1:8100/"
  title="Bond Factor Lab"
  style="width: 100%; height: 100%; border: 0;"
></iframe>
```

外层容器建议保证:

- iframe 父容器高度为视口高度或 shell 内容区高度。
- 不额外裁剪滚动区域，避免因子实验室表格和抽屉被截断。
- 保留当前页面路由或菜单项名称为“因子实验室”。

## 跨机器访问注意

当前 backend launchd 配置监听 `127.0.0.1:8100`，适合同机嵌入。如果 panda_quantflow 页面在其他机器浏览器中打开，`127.0.0.1` 会指向访问者自己的电脑，不会指向测试 Mac。

跨机器访问时需要二选一:

1. 将 backend 监听地址改为测试 Mac 内网地址或 `0.0.0.0`，并在 iframe 中使用 `http://<Mac-Studio-IP>:8100/`。
2. 由 panda_quantflow 后端做同机反向代理，把 `/bond-factor-lab/` 转发到 `http://127.0.0.1:8100/`。

## 验收点

- iframe 内能看到“预测准确率矩阵”。
- 默认任务显示 `3Y · T+5`。
- T+1/T+5 任务格子能展示当前可用回测数据；周度任务格子当前无数据（周度方案已退役）。
- 浏览器 console 无红色错误。
- 外层 shell 的导航、滚动、路由切换不影响 iframe 内部交互。

## 当前阻塞

本机未找到可直接修改的 `panda_quantflow` 仓库路径；当前仓库已完成可嵌入服务侧准备。拿到外层仓库路径后，只需把上述 iframe 地址接入对应菜单/路由，并按验收点验证。
