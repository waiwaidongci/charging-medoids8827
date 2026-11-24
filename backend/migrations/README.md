初始结构由 `src/models/tables.py` 定义，在 API 启动前由 `metadata.create_all` 创建。演示数据通过独立入口填充。

后续新增的列（如 `analysis_runs.meter_refs`、`analysis_runs.invalidated`）由 `src/main.py` 的 `ensure_columns` 在启动时按缺失情况 `ALTER TABLE` 补齐；新增表（如 `meter_records`）仍由 `metadata.create_all` 创建。
