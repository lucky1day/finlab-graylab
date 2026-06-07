# Reports

本目录用于保存本机生成的对比报告和审计明细。CSV/JSON 报告通常体积较大且可再生成，不进入 Git 管理。

常见生成入口:

- `scripts/audit_daily_data_service.py`
- `scripts/compare_wind_export_weekly.py`
- `scripts/generate_data_diff_report.py`

如文档需要引用某次报告结论，应在 `docs/` 中记录摘要、命令和关键输出，而不是依赖本目录下的临时产物被提交。
