"""raw_wind_v1 导出器在真实 MySQL 上：DECIMAL/DATETIME/JSON/NULL 与流式游标，读回必须与 read_sql 逐位相同。"""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import text

from shared.data_bridge.raw_wind import (
    RAW_WIND_TABLES,
    export_raw_wind,
    read_raw_wind_frame,
    verify_raw_wind_artifact,
)

pytestmark = pytest.mark.mysql_integration

_LONG = "id VARCHAR(40) PRIMARY KEY, rdate VARCHAR(10), {}indicators_name VARCHAR(256), indicators_code VARCHAR(128), indicators_value DECIMAL(30,12), create_time DATETIME"


def _create_source_tables(engine) -> None:
    with engine.begin() as c:
        for name in ("api_wind_daily", "api_wind_derivative_daily", "api_wind_monthly"):
            c.execute(text(f"CREATE TABLE {name} ({_LONG.format('')})"))
        for name in ("api_wind_weekly", "api_wind_derivative_weekly"):
            c.execute(text(f"CREATE TABLE {name} ({_LONG.format('week_id VARCHAR(10), ')})"))
        c.execute(text(f"CREATE TABLE api_wind_derivative_monthly ({_LONG.format('month_id VARCHAR(10), ')})"))
        c.execute(text("CREATE TABLE api_wind_date (rdate VARCHAR(10), week_id VARCHAR(10))"))
        c.execute(text("CREATE TABLE t_trade_calendar (rdate VARCHAR(10), trade_flag VARCHAR(2))"))
        c.execute(text("CREATE TABLE api_wind_indicators_all (id VARCHAR(40) PRIMARY KEY, indicators_code VARCHAR(128), remark1 VARCHAR(500), daily_model JSON, create_time DATETIME, lag_length VARCHAR(10))"))
        for name in ("api_wind_daily", "api_wind_derivative_daily", "api_wind_monthly"):
            c.execute(text(f"INSERT INTO {name} VALUES ('2','2026-11-02','n','B',2.500000000000,'2026-01-01 00:00:00'),('1','2026-11-02','n','A',0.123456789012,'2026-01-01 00:00:00'),('3','2026-11-01','n','A',NULL,NULL),('4','2026-11-01','n','A',-1234567.000000000001,NULL)"))
        for name in ("api_wind_weekly", "api_wind_derivative_weekly", "api_wind_derivative_monthly"):
            c.execute(text(f"INSERT INTO {name} VALUES ('1','2026-11-06','202645','n','A',1.5,NULL)"))
        c.execute(text("INSERT INTO api_wind_date VALUES ('2026-11-02','202645'),('2026-11-01',NULL)"))
        c.execute(text("INSERT INTO t_trade_calendar VALUES ('2026-11-02','1'),('2026-11-01','0')"))
        c.execute(text("""INSERT INTO api_wind_indicators_all VALUES
            ('a','A','',JSON_OBJECT('k',1),'2026-01-31 16:58:48','5'),('b','B',NULL,NULL,NULL,'\\\\N')"""))


def test_export_round_trips_real_mysql_types(empty_mysql_engine, tmp_path) -> None:
    engine = empty_mysql_engine
    _create_source_tables(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        artifact = export_raw_wind(connection, tmp_path, generation_id="g", refresh_date="2026-11-12")
        connection.rollback()
    verify_raw_wind_artifact(artifact.path)
    with engine.connect() as connection:
        for table in RAW_WIND_TABLES:
            columns = "*" if table.columns is None else ", ".join(table.columns)
            where = " AND ".join(f"{c} IS NOT NULL" for c in table.non_null)
            sql = f"SELECT {columns} FROM {table.name}" + (f" WHERE {where}" if where else "") + f" ORDER BY {', '.join(table.order_by)}"
            pd.testing.assert_frame_equal(
                read_raw_wind_frame(artifact.path, table.name), pd.read_sql(sql, connection), check_dtype=True, check_exact=True
            )


def test_prepare_raw_wind_sidecar_publishes_a_ready_artifact_for_the_generation(empty_mysql_engine, tmp_path) -> None:
    from shared.input_artifacts import (
        get_ready_blackbox_snapshot, get_ready_raw_wind_artifact, prepare_blackbox_generation_snapshot, prepare_raw_wind_sidecar,
    )
    from tests.test_factor_versioned_inputs import _dataset_and_state, _frames

    engine = empty_mysql_engine
    _create_source_tables(engine)
    dataset, state = _dataset_and_state(_frames(include_v2=True))
    cache = tmp_path / "cache"
    prepare_blackbox_generation_snapshot(state=state, dataset=dataset, cache_root=cache)
    snapshot = get_ready_blackbox_snapshot(snapshot_date="2026-08-28", cache_root=cache, factor_input_mode="algorithm_managed")

    artifact = prepare_raw_wind_sidecar(state=state, engine=engine, root=tmp_path / "raw")

    ready = get_ready_raw_wind_artifact(snapshot=snapshot, root=tmp_path / "raw")
    assert ready.artifact_id == artifact.artifact_id
    assert artifact.manifest["generation_id"] == snapshot.generation_id
