"""raw_wind_v1 旁路产物：确定性、NULL 与反斜杠编码、内容寻址、篡改与缺失检测。"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from shared.data_bridge.raw_wind import (
    MANIFEST_FILENAME,
    RAW_WIND_FILENAMES,
    RAW_WIND_TABLES,
    RawWindError,
    decode_cell,
    encode_cell,
    export_raw_wind,
    raw_wind_filename,
    read_raw_wind_manifest,
    verify_raw_wind_artifact,
)

_LONG_DDL = "CREATE TABLE {} (id INTEGER PRIMARY KEY, rdate TEXT, {}indicators_code TEXT, indicators_value REAL, extra TEXT)"


def _engine():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        for name in ("api_wind_daily", "api_wind_derivative_daily", "api_wind_monthly"):
            c.execute(text(_LONG_DDL.format(name, "")))
        c.execute(text(_LONG_DDL.format("api_wind_weekly", "week_id TEXT, ")))
        c.execute(text(_LONG_DDL.format("api_wind_derivative_weekly", "week_id TEXT, ")))
        c.execute(text(_LONG_DDL.format("api_wind_derivative_monthly", "month_id TEXT, ")))
        c.execute(text("CREATE TABLE api_wind_date (rdate TEXT, week_id TEXT)"))
        c.execute(text("CREATE TABLE t_trade_calendar (rdate TEXT, trade_flag TEXT)"))
        c.execute(text("CREATE TABLE api_wind_indicators_all (id TEXT PRIMARY KEY, indicators_code TEXT, remark1 TEXT, lag_length TEXT)"))
        # 长表：乱序插入，含 NULL 值行（应被过滤）与不在导出列中的额外列
        for table in ("api_wind_daily", "api_wind_derivative_daily", "api_wind_monthly"):
            c.execute(text(f"INSERT INTO {table} VALUES (2, '2026-11-02', 'B', 2.5, 'x'), (1, '2026-11-02', 'A', 0.1, 'x'), (3, '2026-11-01', 'A', NULL, 'x'), (4, '2026-11-01', 'A', 1e-07, 'x')"))
        for table in ("api_wind_weekly", "api_wind_derivative_weekly"):
            c.execute(text(f"INSERT INTO {table} VALUES (1, '2026-11-06', '202645', 'A', 1.5, 'x')"))
        c.execute(text("INSERT INTO api_wind_derivative_monthly VALUES (1, '2026-10-30', '202610', 'A', 3.0, 'x')"))
        c.execute(text("INSERT INTO api_wind_date VALUES ('2026-11-02', '202645'), ('2026-11-01', NULL), ('2026-11-03', '202645')"))
        c.execute(text("INSERT INTO t_trade_calendar VALUES ('2026-11-03', '1'), ('2026-11-01', '0')"))
        c.execute(text("INSERT INTO api_wind_indicators_all VALUES ('b', 'B', '', NULL), ('a', 'A', '\\N', '\\\\path')"))
    return engine


def _export(engine, root: Path, **kw):
    with engine.connect() as connection:
        return export_raw_wind(connection, root, generation_id="gen-1", refresh_date="2026-11-12", **kw)


def _read(artifact, table: str) -> list[list[str | None]]:
    path = artifact.path / raw_wind_filename(table)
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"), newline="")))
    return [rows[0], *[[decode_cell(cell) for cell in row] for row in rows[1:]]]


def test_export_is_content_addressed_sealed_and_verifiable(tmp_path):
    artifact = _export(_engine(), tmp_path)

    assert artifact.path == tmp_path / artifact.artifact_id and artifact.artifact_id.startswith("raw-wind-")
    assert sorted(p.name for p in artifact.path.iterdir()) == sorted([*RAW_WIND_FILENAMES, MANIFEST_FILENAME])
    assert verify_raw_wind_artifact(artifact.path)["artifact_id"] == artifact.artifact_id
    assert read_raw_wind_manifest(artifact.path)["generation_id"] == "gen-1"
    assert (artifact.path.stat().st_mode & 0o222) == 0 and all((p.stat().st_mode & 0o222) == 0 for p in artifact.path.iterdir())
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".raw-wind-staging-")]


def test_rows_columns_filters_and_order_follow_the_table_specs(tmp_path):
    artifact = _export(_engine(), tmp_path)

    assert _read(artifact, "api_wind_daily") == [
        ["rdate", "indicators_code", "indicators_value"],
        ["2026-11-01", "A", "1e-07"],
        ["2026-11-02", "A", "0.1"],
        ["2026-11-02", "B", "2.5"],
    ]
    assert _read(artifact, "api_wind_derivative_monthly")[0] == ["rdate", "month_id", "indicators_code", "indicators_value"]
    assert _read(artifact, "api_wind_date") == [["rdate", "week_id"], ["2026-11-02", "202645"], ["2026-11-03", "202645"]]
    assert _read(artifact, "t_trade_calendar") == [["rdate", "trade_flag"], ["2026-11-01", "0"], ["2026-11-03", "1"]]
    manifest = artifact.manifest["tables"]
    assert manifest["api_wind_daily"]["rows"] == 3 and manifest["api_wind_daily"]["kinds"]["indicators_value"] == "float"
    assert manifest["api_wind_indicators_all"]["columns"] == ["id", "indicators_code", "remark1", "lag_length"]


def test_null_empty_string_and_backslash_values_round_trip_exactly(tmp_path):
    artifact = _export(_engine(), tmp_path)

    assert _read(artifact, "api_wind_indicators_all")[1:] == [
        ["a", "A", "\\N", "\\\\path"],  # 字面量 \N 与以反斜杠开头的字符串被还原，不会被当成 NULL
        ["b", "B", "", None],  # 空字符串与 NULL 可区分
    ]


def test_export_is_deterministic_and_idempotent(tmp_path):
    first = _export(_engine(), tmp_path / "one")
    second = _export(_engine(), tmp_path / "two")
    again = _export(_engine(), tmp_path / "one")

    assert first.artifact_id == second.artifact_id == again.artifact_id
    assert [p.name for p in (tmp_path / "one").iterdir()] == [first.artifact_id]


def test_generation_identity_is_part_of_the_artifact_identity(tmp_path):
    engine = _engine()
    with engine.connect() as connection:
        one = export_raw_wind(connection, tmp_path, generation_id="gen-1", refresh_date="2026-11-12")
        two = export_raw_wind(connection, tmp_path, generation_id="gen-2", refresh_date="2026-11-12")
    assert one.artifact_id != two.artifact_id


@pytest.mark.parametrize("damage", ["modify", "delete", "extra", "manifest"])
def test_verification_rejects_tampered_incomplete_or_extended_artifacts(tmp_path, damage):
    artifact = _export(_engine(), tmp_path)
    artifact.path.chmod(0o755)
    target = artifact.path / raw_wind_filename("api_wind_daily")
    if damage == "modify":
        target.chmod(0o644)
        target.write_text(target.read_text() + "2026-11-09,Z,9\n")
    elif damage == "delete":
        target.unlink()
    elif damage == "extra":
        (artifact.path / "extra.csv").write_text("x")
    else:
        manifest = artifact.path / MANIFEST_FILENAME
        manifest.chmod(0o644)
        manifest.write_text(manifest.read_text().replace("gen-1", "gen-9"))

    with pytest.raises(RawWindError):
        verify_raw_wind_artifact(artifact.path)


def test_cell_encoding_covers_every_supported_value_type():
    assert encode_cell(None) == ("\\N", None)
    assert encode_cell(Decimal("1.2300")) == ("1.23", "float")
    assert encode_cell(0.1) == ("0.1", "float")
    assert encode_cell(7) == ("7", "int")
    assert encode_cell(True) == ("1", "int")
    assert encode_cell(datetime(2026, 1, 31, 16, 58, 48)) == ("2026-01-31 16:58:48", "datetime")
    assert encode_cell(date(2026, 1, 31)) == ("2026-01-31", "datetime")
    assert encode_cell("plain") == ("plain", "str") and encode_cell("") == ("", "str")
    assert encode_cell("\\N") == ("\\\\N", "str")
    for original in ("\\N", "\\\\x", "\\", "plain", ""):
        assert decode_cell(encode_cell(original)[0]) == original
    with pytest.raises(RawWindError):
        encode_cell(b"binary")


def test_table_specs_cover_exactly_the_nine_source_tables():
    assert [t.name for t in RAW_WIND_TABLES] == [
        "api_wind_daily", "api_wind_derivative_daily", "api_wind_weekly", "api_wind_derivative_weekly",
        "api_wind_monthly", "api_wind_derivative_monthly", "api_wind_date", "t_trade_calendar", "api_wind_indicators_all",
    ]
    assert len(RAW_WIND_FILENAMES) == 9


def test_reference_reader_matches_pandas_read_sql_for_every_table(tmp_path):
    import pandas as pd
    from shared.data_bridge.raw_wind import read_raw_wind_frame

    engine = _engine()
    artifact = _export(engine, tmp_path)
    with engine.connect() as connection:
        for table in RAW_WIND_TABLES:
            columns = "*" if table.columns is None else ", ".join(table.columns)
            where = " AND ".join(f"{c} IS NOT NULL" for c in table.non_null)
            sql = f"SELECT {columns} FROM {table.name}" + (f" WHERE {where}" if where else "") + f" ORDER BY {', '.join(table.order_by)}"
            expected = pd.read_sql(sql, connection)
            pd.testing.assert_frame_equal(read_raw_wind_frame(artifact.path, table.name), expected, check_dtype=True, check_exact=True)
