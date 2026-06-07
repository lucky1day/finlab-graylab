# Reports

本目录用于保存本机生成的对比报告和审计明细。CSV/JSON 报告通常体积较大且可再生成，不进入 Git 管理。

常见生成入口:

- `scripts/audit_daily_data_service.py`
- `scripts/compare_weekly_wind_export.py`
- `scripts/generate_daily_data_diff_report.py`

如文档需要引用某次报告结论，应在 `docs/` 中记录摘要、命令和关键输出，而不是依赖本目录下的临时产物被提交。

## 命名规范

- 报告文件使用小写 snake_case。
- 对比报告使用 `{domain}_{left}_vs_{right}_{result}.csv|json|html`，例如 `weekly_output_csv_vs_current_db_material_diff.csv`。
- 版本日期必须独立成段，例如 `weekly_output_0521_csv_vs_current_db_material_diff.csv`，不要写成 `weekly_output0521_...`。
- 摘要文件以 `_summary.json` 结尾，逐项差异以 `_diff.csv` 结尾，生成出的输入快照以 `_generated.csv` 结尾。
- 如果同一批报告需要长期保留，应把关键结论写入 `docs/`，并把原始大文件留在本目录的 ignored 产物中。
