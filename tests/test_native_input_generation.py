from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import shutil
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd


_CONTRACT_CUTOFF = datetime(
    2026,
    7,
    23,
    22,
    30,
    tzinfo=timezone.utc,
)
_CAPTURE_DEADLINE = datetime(
    2026,
    7,
    23,
    22,
    31,
    tzinfo=timezone.utc,
)
_SNAPSHOT_STARTED = datetime(
    2026,
    7,
    23,
    22,
    30,
    15,
    tzinfo=timezone.utc,
)


def _clock_contract_kwargs() -> dict[str, object]:
    return {
        "source_contract_cutoff": _CONTRACT_CUTOFF,
        "capture_not_after": _CAPTURE_DEADLINE,
        "_snapshot_clock": lambda: _SNAPSHOT_STARTED,
    }


class _Rows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> "_Rows":
        return self

    def all(self) -> list[dict[str, object]]:
        return list(self._rows)


class _Connection:
    def __init__(self) -> None:
        self.driver_sql: list[str] = []
        self.queries: list[str] = []
        self.rollback_count = 0
        self.commit_count = 0
        self.source_commit_token = "a" * 64

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def exec_driver_sql(self, statement: str) -> None:
        self.driver_sql.append(statement)

    def execute(
        self,
        statement: object,
        _params: dict[str, object] | None = None,
    ) -> _Rows:
        sql = str(statement)
        self.queries.append(sql)
        if "FROM api_wind_date" in sql:
            return _Rows(
                [
                    {"rdate": "2026-07-23", "week_id": "202629"},
                    {"rdate": "2026-07-24", "week_id": "202629"},
                    {"rdate": "2026-07-27", "week_id": "202630"},
                ]
            )
        if "FROM t_trade_calendar" in sql:
            return _Rows(
                [
                    {"rdate": "2026-07-23", "trade_flag": "1"},
                    {"rdate": "2026-07-24", "trade_flag": "1"},
                    {"rdate": "2026-07-25", "trade_flag": "0"},
                    {"rdate": "2026-07-26", "trade_flag": "0"},
                    {"rdate": "2026-07-27", "trade_flag": "1"},
                ]
            )
        raise AssertionError(f"unexpected query: {sql}")

    def rollback(self) -> None:
        self.rollback_count += 1

    def commit(self) -> None:
        self.commit_count += 1


class _Engine:
    def __init__(self) -> None:
        self.connection = _Connection()
        self.connect_count = 0

    def connect(self) -> _Connection:
        self.connect_count += 1
        return self.connection


def _metadata() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "indicators_code": code,
                "frequency": "daily",
                "status": 1,
                "pre_forecast_flag": 1,
                "lag_length": 0,
                "indicators_source": "raw",
            }
            for code in (
                "TB1YWI0C",
                "TB3YWI0C",
                "TB5YWI0C",
                "TB7YWI0C",
                "TB0YWI0C",
            )
        ]
        + [
            {
                "indicators_code": "WEEKLY_A",
                "frequency": "weekly",
                "status": 1,
                "pre_forecast_flag": 1,
                "lag_length": 0,
                "indicators_source": "raw",
            },
            {
                "indicators_code": "MONTHLY_A",
                "frequency": "monthly",
                "status": 1,
                "pre_forecast_flag": 1,
                "lag_length": 0,
                "indicators_source": "raw",
            },
        ]
    )


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "rdate": "2026-07-24",
                "indicators_code": code,
                "indicators_value": 1.5,
            }
            for code in (
                "TB1YWI0C",
                "TB3YWI0C",
                "TB5YWI0C",
                "TB7YWI0C",
                "TB0YWI0C",
            )
        ]
        + [
            {
                "rdate": "2026-07-25",
                "indicators_code": "TB1YWI0C",
                "indicators_value": 9.9,
            },
        ]
    )


def _weekly_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "rdate": "2026-07-24",
                "week_id": "202629",
                "indicators_code": "WEEKLY_A",
                "indicators_value": 2.5,
            },
            {
                "rdate": "2026-07-25",
                "week_id": "202629",
                "indicators_code": "WEEKLY_A",
                "indicators_value": 8.8,
            },
        ]
    )


def _monthly_frame(*, include_month_id: bool) -> pd.DataFrame:
    rows: list[dict[str, object]] = [
        {
            "rdate": "2026-07-15",
            "indicators_code": "MONTHLY_A",
            "indicators_value": 3.5,
        },
        {
            "rdate": "2026-07-25",
            "indicators_code": "MONTHLY_A",
            "indicators_value": 7.7,
        },
    ]
    if include_month_id:
        for row in rows:
            row["month_id"] = "202607"
    columns = ["rdate", "indicators_code", "indicators_value"]
    if include_month_id:
        columns.insert(1, "month_id")
    return pd.DataFrame(rows, columns=columns)


def _patched_source_readers(
    module,
    connection: _Connection,
    *,
    evidence_timestamp: str = "2026-07-24T06:29:00.000000",
):
    seen_connections: list[object] = []

    def metadata_reader(engine: object) -> pd.DataFrame:
        seen_connections.append(engine)
        return _metadata()

    def daily_reader(
        _codes: object,
        _table_name: str,
        engine: object,
    ) -> pd.DataFrame:
        seen_connections.append(engine)
        return _daily_frame()

    def weekly_reader(
        _codes: object,
        _table_name: str,
        engine: object,
    ) -> pd.DataFrame:
        seen_connections.append(engine)
        return _weekly_frame()

    def monthly_reader(
        _codes: object,
        _table_name: str,
        engine: object,
        include_month_id: bool = False,
    ) -> pd.DataFrame:
        seen_connections.append(engine)
        return _monthly_frame(include_month_id=include_month_id)

    def evidence_reader(
        engine: object,
        *,
        feature_date: str,
    ):
        seen_connections.append(engine)
        seed = int(connection.source_commit_token[0], 16) + 1
        factor_tables = tuple(module._data_contract.FACTOR_SOURCE_TABLES)
        calendar_tables = tuple(module._data_contract.CALENDAR_SOURCE_TABLES)
        tables = [
            module._data_contract.SourceTableEvidence(
                table_name=table_name,
                row_count=seed,
                latest_create_time=evidence_timestamp,
            )
            for table_name in factor_tables
        ]
        tables.append(
            module._data_contract.SourceTableEvidence(
                table_name=module._data_contract.METADATA_SOURCE_TABLE,
                row_count=seed,
                latest_create_time=evidence_timestamp,
                latest_update_time=evidence_timestamp,
            )
        )
        tables.extend(
            module._data_contract.SourceTableEvidence(
                table_name=table_name,
                row_count=seed,
                latest_create_time=None,
                latest_business_key=feature_date,
            )
            for table_name in calendar_tables
        )
        ordered = tuple(sorted(tables, key=lambda item: item.table_name))
        payload = {
            "evidence_version": "native-source-watermark-v1",
            "feature_date": feature_date,
            "tables": [
                {
                    "table_name": item.table_name,
                    "row_count": item.row_count,
                    "latest_create_time": item.latest_create_time,
                    "latest_update_time": item.latest_update_time,
                    "latest_business_key": item.latest_business_key,
                }
                for item in ordered
            ],
        }
        token = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
        return module._data_contract.SourceCommitEvidence(
            feature_date=feature_date,
            source_commit_token=token,
            tables=ordered,
        )

    stack = [
        patch.object(
            module._data_service,
            "read_factor_metadata_from_db",
            side_effect=metadata_reader,
        ),
        patch.object(
            module._data_service,
            "read_daily_long_from_db",
            side_effect=daily_reader,
        ),
        patch.object(
            module._data_service,
            "read_weekly_long_from_db",
            side_effect=weekly_reader,
        ),
        patch.object(
            module._data_service,
            "read_monthly_long_from_db",
            side_effect=monthly_reader,
        ),
        patch.object(
            module._data_contract,
            "capture_source_commit_evidence_from_connection",
            side_effect=evidence_reader,
        ),
    ]
    return stack, seen_connections


def _create_generation(
    module,
    output_root: str | Path,
    *,
    source_commit_token: str = "a" * 64,
    readiness_basis: str = "CLOCK_CONTRACT",
    **generation_options: object,
):
    engine = _Engine()
    engine.connection.source_commit_token = source_commit_token
    patches, _ = _patched_source_readers(module, engine.connection)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        context = module.create_native_generation(
            engine,
            business_date="2026-07-24",
            feature_date="2026-07-24",
            output_root=output_root,
            readiness_basis=readiness_basis,
            **_clock_contract_kwargs(),
            **generation_options,
        )
    return engine, context


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _make_generation_writable(root: Path) -> None:
    root.chmod(0o755)
    for path in root.iterdir():
        if not path.is_symlink():
            path.chmod(0o644)


def _rewrite_manifest(
    context,
    mutate,
    *,
    canonical: bool = True,
) -> dict[str, object]:
    manifest = context.manifest
    mutate(manifest)
    identity = {
        "schema_version": manifest["schema_version"],
        "files": manifest["files"],
    }
    dataset_content_id = hashlib.sha256(
        _canonical_json_bytes(identity)
    ).hexdigest()
    manifest["dataset_content_id"] = dataset_content_id
    stable_provenance = {
        "generation_type": manifest["generation_type"],
        "dataset_content_id": dataset_content_id,
        "source_commit_token": manifest["source_commit_token"],
        "business_date": manifest["business_date"],
        "feature_date": manifest["feature_date"],
        "readiness_basis": manifest["readiness_basis"],
        "schema_version": manifest["schema_version"],
        "exporter_version": manifest["exporter_version"],
    }
    generation_digest = hashlib.sha256(
        _canonical_json_bytes(stable_provenance)
    ).hexdigest()
    manifest["generation_id"] = f"native-{generation_digest[:24]}"
    if canonical:
        rendered = _canonical_json_bytes(manifest) + b"\n"
    else:
        rendered = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False)
            + "\n"
        ).encode("utf-8")
    context.manifest_path.write_bytes(rendered)
    return manifest


def _rewrite_csv_and_manifest(
    context,
    filename: str,
    frame: pd.DataFrame,
) -> None:
    raw = frame.to_csv(index=False, lineterminator="\n", na_rep="").encode(
        "utf-8"
    )
    path = context.root_dir / filename
    path.write_bytes(raw)

    def mutate(manifest: dict[str, object]) -> None:
        files = manifest["files"]
        entry = files[filename]
        entry["sha256"] = hashlib.sha256(raw).hexdigest()
        entry["size_bytes"] = len(raw)
        entry["row_count"] = len(frame)
        entry["columns"] = list(frame.columns)

    _rewrite_manifest(context, mutate)


class NativeInputGenerationTransactionTests(unittest.TestCase):
    def test_frozen_snapshot_rejects_partial_curve_anchor_set(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        daily = pd.DataFrame(
            [
                {
                    "rdate": "2026-07-24",
                    "indicators_code": code,
                    "indicators_value": 1.0,
                }
                for code in ("TB1YWI0C", "TB5YWI0C", "TB0YWI0C")
            ]
        )
        empty_daily = daily.iloc[0:0].copy()
        frames = {
            "api_wind_daily.csv": daily,
            "api_wind_derivative_daily.csv": empty_daily,
            "weekly_cutoff_index.csv": pd.DataFrame(
                [
                    {
                        "week_id": "202629",
                        "available_date": "2026-07-24",
                    }
                ]
            ),
            "monthly_cutoff_index.csv": pd.DataFrame(
                [
                    {
                        "month_id": "202607",
                        "available_date": "2026-07-24",
                    }
                ]
            ),
        }

        with self.assertRaisesRegex(
            ValueError,
            "missing required daily anchors.*TB3YWI0C.*TB7YWI0C",
        ):
            module._derive_cutoffs(
                frames,
                feature_date="2026-07-24",
            )

    def test_clock_contract_requires_explicit_cutoff_and_capture_deadline(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        engine = _Engine()
        patches, _ = _patched_source_readers(module, engine.connection)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                with self.assertRaisesRegex(
                    ValueError,
                    "CLOCK_CONTRACT requires",
                ):
                    module.create_native_generation(
                        engine,
                        business_date="2026-07-24",
                        feature_date="2026-07-24",
                        output_root=tmpdir,
                    )

        self.assertEqual(engine.connect_count, 0)

    def test_generation_accepts_ready_snapshot_after_0630_not_before(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        engine = _Engine()
        patches, _ = _patched_source_readers(
            module,
            engine.connection,
            evidence_timestamp="2026-07-24T06:42:00.000000",
        )
        capture_deadline = datetime(
            2026,
            7,
            23,
            23,
            55,
            tzinfo=timezone.utc,
        )
        snapshot_started = datetime(
            2026,
            7,
            23,
            22,
            43,
            tzinfo=timezone.utc,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                context = module.create_native_generation(
                    engine,
                    business_date="2026-07-24",
                    feature_date="2026-07-24",
                    output_root=tmpdir,
                    source_contract_cutoff=_CONTRACT_CUTOFF,
                    capture_not_after=capture_deadline,
                    _snapshot_clock=lambda: snapshot_started,
                )
            reopened = module.open_native_generation(
                context.manifest_path,
                expected_manifest_sha256=context.manifest_sha256,
            )

        self.assertEqual(reopened.generation_id, context.generation_id)
        self.assertEqual(
            context.manifest["snapshot_started_at"],
            "2026-07-23T22:43:00.000000Z",
        )

    def test_native_exporter_rejects_upstream_seal_readiness(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        engine = _Engine()
        patches, _ = _patched_source_readers(module, engine.connection)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                with self.assertRaisesRegex(
                    ValueError,
                    "UPSTREAM_SEAL",
                ):
                    module.create_native_generation(
                        engine,
                        business_date="2026-07-24",
                        feature_date="2026-07-24",
                        output_root=tmpdir,
                        readiness_basis="UPSTREAM_SEAL",
                    )

        self.assertEqual(engine.connect_count, 0)

    def test_generation_rejects_daily_target_cutoff_before_feature_date(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        engine = _Engine()
        patches, _ = _patched_source_readers(module, engine.connection)
        stale_daily = pd.DataFrame(
            [
                {
                    "rdate": "2026-07-23",
                    "indicators_code": "TB1YWI0C",
                    "indicators_value": 1.5,
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patches[0],
                patch.object(
                    module._data_service,
                    "read_daily_long_from_db",
                    return_value=stale_daily,
                ),
                patches[2],
                patches[3],
                patches[4],
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "daily cutoff must equal feature_date",
                ):
                    module.create_native_generation(
                        engine,
                        business_date="2026-07-24",
                        feature_date="2026-07-24",
                        output_root=tmpdir,
                        **_clock_contract_kwargs(),
                    )

    def test_factor_cutoff_normalizes_mixed_mysql_date_representations(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        frame = pd.DataFrame(
            [
                {
                    "rdate": "2026-07-23",
                    "week_id": "202629",
                    "indicators_code": "WEEKLY_A",
                    "indicators_value": 1.0,
                },
                {
                    "rdate": "2026-07-24 00:00:00",
                    "week_id": "202629",
                    "indicators_code": "WEEKLY_A",
                    "indicators_value": 2.0,
                },
            ]
        )

        frozen = module._cutoff_factor_frame(
            "api_wind_derivative_weekly.csv",
            frame,
            "2026-07-24",
        )

        self.assertEqual(
            frozen["rdate"].tolist(),
            ["2026-07-23", "2026-07-24"],
        )

    def test_every_source_is_exported_from_one_read_only_rr_connection(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        engine = _Engine()
        patches, seen_connections = _patched_source_readers(
            module,
            engine.connection,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
            ):
                context = module.create_native_generation(
                    engine,
                    business_date="2026-07-24",
                    feature_date="2026-07-24",
                    output_root=tmpdir,
                    **_clock_contract_kwargs(),
                )

            self.assertEqual(engine.connect_count, 1)
            self.assertEqual(
                engine.connection.driver_sql,
                [
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
                    "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
                ],
            )
            self.assertEqual(engine.connection.rollback_count, 1)
            self.assertEqual(engine.connection.commit_count, 0)
            self.assertTrue(seen_connections)
            self.assertTrue(
                all(item is engine.connection for item in seen_connections)
            )
            self.assertEqual(
                {path.name for path in context.root_dir.iterdir()},
                {
                    "manifest.json",
                    "metadata.csv",
                    "api_wind_daily.csv",
                    "api_wind_derivative_daily.csv",
                    "api_wind_weekly.csv",
                    "api_wind_derivative_weekly.csv",
                    "api_wind_monthly.csv",
                    "api_wind_derivative_monthly.csv",
                    "api_wind_date.csv",
                    "t_trade_calendar.csv",
                    "weekly_cutoff_index.csv",
                    "monthly_cutoff_index.csv",
                },
            )
            self.assertEqual(
                context.frame("api_wind_daily")["rdate"].tolist(),
                ["2026-07-24"] * 5,
            )
            self.assertEqual(
                context.frame("api_wind_weekly")["rdate"].tolist(),
                ["2026-07-24"],
            )
            self.assertEqual(
                context.frame("api_wind_monthly")["rdate"].tolist(),
                ["2026-07-15"],
            )

    def test_snapshot_capture_rejects_exact_deadline_before_source_reads(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        engine = _Engine()
        patches, seen_connections = _patched_source_readers(
            module,
            engine.connection,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "snapshot capture window expired",
                ):
                    module.create_native_generation(
                        engine,
                        business_date="2026-07-24",
                        feature_date="2026-07-23",
                        output_root=tmpdir,
                        capture_not_after=datetime(
                            2026,
                            7,
                            23,
                            22,
                            31,
                            tzinfo=timezone.utc,
                        ),
                        source_contract_cutoff=_CONTRACT_CUTOFF,
                        _snapshot_clock=lambda: datetime(
                            2026,
                            7,
                            23,
                            22,
                            31,
                            tzinfo=timezone.utc,
                        ),
                    )

            self.assertEqual(engine.connect_count, 1)
            self.assertEqual(engine.connection.rollback_count, 1)
            self.assertEqual(seen_connections, [])
            self.assertEqual(list(Path(tmpdir).iterdir()), [])

    def test_open_rejects_manifest_started_at_exact_capture_deadline(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(module, tmpdir)
            _make_generation_writable(context.root_dir)
            rewritten = _rewrite_manifest(
                context,
                lambda manifest: manifest.__setitem__(
                    "snapshot_started_at",
                    "2026-07-23T22:31:00.000000Z",
                ),
            )

            with self.assertRaisesRegex(
                ValueError,
                "outside the CLOCK_CONTRACT capture window",
            ):
                module.open_native_generation(
                    context.manifest_path,
                    expected_generation_id=str(
                        rewritten["generation_id"]
                    ),
                )

    def test_source_read_failure_rolls_back_and_leaves_no_files(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        engine = _Engine()
        patches, _ = _patched_source_readers(module, engine.connection)
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patches[0],
                patches[1],
                patch.object(
                    module._data_service,
                    "read_weekly_long_from_db",
                    side_effect=RuntimeError("source read failed"),
                ),
                patches[3],
                patches[4],
            ):
                with self.assertRaisesRegex(RuntimeError, "source read failed"):
                    module.create_native_generation(
                        engine,
                        business_date="2026-07-24",
                        feature_date="2026-07-24",
                        output_root=tmpdir,
                        **_clock_contract_kwargs(),
                    )

            self.assertEqual(engine.connection.rollback_count, 1)
            self.assertEqual(list(Path(tmpdir).iterdir()), [])

    def test_publish_failure_removes_building_directory(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        engine = _Engine()
        patches, _ = _patched_source_readers(module, engine.connection)
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
                patch.object(
                    module.os,
                    "replace",
                    side_effect=OSError("injected replace failure"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "injected replace failure"):
                    module.create_native_generation(
                        engine,
                        business_date="2026-07-24",
                        feature_date="2026-07-24",
                        output_root=tmpdir,
                        **_clock_contract_kwargs(),
                    )

            self.assertEqual(engine.connection.rollback_count, 1)
            self.assertEqual(list(Path(tmpdir).iterdir()), [])


class NativeInputGenerationIntegrityTests(unittest.TestCase):
    def test_manifest_persists_clock_contract_and_source_evidence(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        engine = _Engine()
        patches, _ = _patched_source_readers(module, engine.connection)
        cutoff = datetime(
            2026,
            7,
            23,
            22,
            30,
            tzinfo=timezone.utc,
        )
        deadline = datetime(
            2026,
            7,
            23,
            22,
            31,
            tzinfo=timezone.utc,
        )
        snapshot_started = datetime(
            2026,
            7,
            23,
            22,
            30,
            15,
            tzinfo=timezone.utc,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                context = module.create_native_generation(
                    engine,
                    business_date="2026-07-24",
                    feature_date="2026-07-24",
                    output_root=tmpdir,
                    source_contract_cutoff=cutoff,
                    capture_not_after=deadline,
                    _snapshot_clock=lambda: snapshot_started,
                )

            manifest = context.manifest
            self.assertEqual(
                manifest["source_contract_cutoff"],
                "2026-07-23T22:30:00.000000Z",
            )
            self.assertEqual(
                manifest["capture_not_after"],
                "2026-07-23T22:31:00.000000Z",
            )
            self.assertEqual(
                manifest["snapshot_started_at"],
                "2026-07-23T22:30:15.000000Z",
            )
            self.assertEqual(
                manifest["source_evidence"]["feature_date"],
                "2026-07-24",
            )
            evidence_digest = hashlib.sha256(
                _canonical_json_bytes(manifest["source_evidence"])
            ).hexdigest()
            self.assertEqual(
                manifest["source_evidence_sha256"],
                evidence_digest,
            )
            self.assertEqual(
                manifest["source_commit_token"],
                evidence_digest,
            )

    def test_manifest_and_context_expose_approved_generation_provenance(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(module, tmpdir)

            self.assertEqual(context.generation_type, "native_source")
            self.assertRegex(
                context.created_at,
                r"^\d{4}-\d{2}-\d{2}T.*Z$",
            )
            self.assertRegex(
                context.sealed_at,
                r"^\d{4}-\d{2}-\d{2}T.*Z$",
            )
            self.assertLessEqual(context.created_at, context.sealed_at)
            self.assertEqual(
                context.cutoffs,
                {
                    "daily": "2026-07-24",
                    "weekly": "202629",
                    "monthly": "202607",
                },
            )
            self.assertEqual(
                set(context.manifest),
                {
                    "manifest_version",
                    "generation_id",
                    "generation_type",
                    "dataset_content_id",
                    "business_date",
                    "feature_date",
                    "readiness_basis",
                    "source_commit_token",
                    "source_contract_cutoff",
                    "capture_not_after",
                    "snapshot_started_at",
                    "source_evidence",
                    "source_evidence_sha256",
                    "schema_version",
                    "exporter_version",
                    "created_at",
                    "sealed_at",
                    "cutoffs",
                    "files",
                },
            )

    def test_repeated_create_reuses_original_manifest_despite_new_timestamps(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        timestamps = [
            "2026-07-24T06:30:00.000001Z",
            "2026-07-24T06:31:00.000001Z",
            "2026-07-24T06:40:00.000001Z",
            "2026-07-24T06:41:00.000001Z",
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                module,
                "_utc_now_text",
                side_effect=timestamps,
                create=True,
            ):
                _, first = _create_generation(module, tmpdir)
                _, second = _create_generation(module, tmpdir)

            self.assertEqual(second.generation_id, first.generation_id)
            self.assertEqual(
                second.dataset_content_id,
                first.dataset_content_id,
            )
            self.assertEqual(second.manifest_sha256, first.manifest_sha256)
            self.assertEqual(
                second.created_at,
                "2026-07-24T06:30:00.000001Z",
            )
            self.assertEqual(
                second.sealed_at,
                "2026-07-24T06:31:00.000001Z",
            )

    def test_generation_id_uses_stable_provenance_not_only_dataset_content(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as first_root:
            _, first = _create_generation(
                module,
                first_root,
                source_commit_token="a" * 64,
            )
        with tempfile.TemporaryDirectory() as second_root:
            _, second = _create_generation(
                module,
                second_root,
                source_commit_token="b" * 64,
            )

        self.assertEqual(
            second.dataset_content_id,
            first.dataset_content_id,
        )
        self.assertNotEqual(second.generation_id, first.generation_id)

    def test_repeated_create_and_open_return_the_same_content_identity(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            first_engine, first = _create_generation(module, tmpdir)
            second_engine, second = _create_generation(module, tmpdir)
            reopened = module.open_native_generation(
                first.manifest_path,
                expected_generation_id=first.generation_id,
                expected_manifest_sha256=first.manifest_sha256,
                expected_business_date="2026-07-24",
                expected_feature_date="2026-07-24",
            )

            self.assertEqual(second.generation_id, first.generation_id)
            self.assertEqual(
                reopened.dataset_content_id,
                first.dataset_content_id,
            )
            self.assertEqual(first_engine.connection.rollback_count, 1)
            self.assertEqual(second_engine.connection.rollback_count, 1)
            self.assertFalse(
                any(
                    path.name.startswith(".building-")
                    for path in Path(tmpdir).iterdir()
                )
            )

    def test_frame_returns_an_isolated_copy(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(module, tmpdir)
            first = context.frame("api_wind_daily")
            first.loc[0, "indicators_value"] = 999

            second = context.frame("api_wind_daily.csv")

            self.assertEqual(second.loc[0, "indicators_value"], 1.5)
            self.assertIsNone(context.dispose())

    def test_open_rejects_file_tampering(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(module, tmpdir)
            _make_generation_writable(context.root_dir)
            path = context.root_dir / "api_wind_daily.csv"
            path.write_bytes(path.read_bytes() + b"\n")

            with self.assertRaisesRegex(ValueError, "file hash mismatch"):
                module.open_native_generation(context.manifest_path)

    def test_open_rejects_manifest_tampering_against_expected_hash(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(module, tmpdir)
            _make_generation_writable(context.root_dir)
            _rewrite_manifest(
                context,
                lambda manifest: manifest.__setitem__(
                    "source_commit_token",
                    "b" * 64,
                ),
            )

            with self.assertRaisesRegex(ValueError, "manifest sha256 mismatch"):
                module.open_native_generation(
                    context.manifest_path,
                    expected_manifest_sha256=context.manifest_sha256,
                )

    def test_open_rejects_rehashed_source_evidence_after_snapshot(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(module, tmpdir)
            _make_generation_writable(context.root_dir)

            def mutate(manifest: dict[str, object]) -> None:
                evidence = manifest["source_evidence"]
                evidence["tables"][0]["latest_create_time"] = (
                    "2026-07-24T07:10:12.000000"
                )
                digest = hashlib.sha256(
                    _canonical_json_bytes(evidence)
                ).hexdigest()
                manifest["source_evidence_sha256"] = digest
                manifest["source_commit_token"] = digest

            _rewrite_manifest(context, mutate)

            with self.assertRaisesRegex(
                RuntimeError,
                "after the supplied cutoff",
            ):
                module.open_native_generation(context.manifest_path)

    def test_open_rejects_noncanonical_manifest(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(module, tmpdir)
            _make_generation_writable(context.root_dir)
            _rewrite_manifest(context, lambda _manifest: None, canonical=False)

            with self.assertRaisesRegex(ValueError, "canonical"):
                module.open_native_generation(context.manifest_path)

    def test_open_rejects_manifest_schema_drift(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(module, tmpdir)
            _make_generation_writable(context.root_dir)
            _rewrite_manifest(
                context,
                lambda manifest: manifest.__setitem__(
                    "schema_version",
                    "native-generation-v2",
                ),
            )

            with self.assertRaisesRegex(ValueError, "schema_version"):
                module.open_native_generation(context.manifest_path)

    def test_open_rejects_extra_manifest_field(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(module, tmpdir)
            _make_generation_writable(context.root_dir)
            _rewrite_manifest(
                context,
                lambda manifest: manifest.__setitem__("unexpected", True),
            )

            with self.assertRaisesRegex(ValueError, "manifest fields"):
                module.open_native_generation(context.manifest_path)

    def test_open_rejects_csv_schema_drift_even_when_manifest_is_rehashed(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(module, tmpdir)
            _make_generation_writable(context.root_dir)
            frame = context.frame("api_wind_daily")
            frame["unexpected"] = "drift"
            _rewrite_csv_and_manifest(context, "api_wind_daily.csv", frame)

            with self.assertRaisesRegex(ValueError, "schema mismatch"):
                module.open_native_generation(context.manifest_path)

    def test_open_rejects_factor_date_after_feature_date_even_when_rehashed(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(module, tmpdir)
            _make_generation_writable(context.root_dir)
            frame = context.frame("api_wind_daily")
            frame.loc[0, "rdate"] = "2026-07-25"
            _rewrite_csv_and_manifest(context, "api_wind_daily.csv", frame)

            with self.assertRaisesRegex(ValueError, "feature_date"):
                module.open_native_generation(context.manifest_path)

    def test_open_rejects_cutoff_drift_even_when_manifest_is_rehashed(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(module, tmpdir)
            _make_generation_writable(context.root_dir)

            def mutate(manifest: dict[str, object]) -> None:
                manifest["cutoffs"]["weekly"] = "202630"

            _rewrite_manifest(context, mutate)
            with self.assertRaisesRegex(ValueError, "cutoffs"):
                module.open_native_generation(context.manifest_path)

    def test_open_rejects_absolute_and_parent_file_paths(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        for unsafe_path in (
            "/tmp/api_wind_daily.csv",
            "../api_wind_daily.csv",
        ):
            with self.subTest(unsafe_path=unsafe_path):
                with tempfile.TemporaryDirectory() as tmpdir:
                    _, context = _create_generation(module, tmpdir)
                    _make_generation_writable(context.root_dir)

                    def mutate(manifest: dict[str, object]) -> None:
                        manifest["files"]["api_wind_daily.csv"]["path"] = (
                            unsafe_path
                        )

                    _rewrite_manifest(context, mutate)
                    with self.assertRaisesRegex(ValueError, "unsafe file path"):
                        module.open_native_generation(context.manifest_path)

    def test_open_rejects_symlinked_file_even_when_bytes_match(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(module, tmpdir)
            _make_generation_writable(context.root_dir)
            target = context.root_dir / "api_wind_daily.csv"
            outside = Path(tmpdir) / "outside.csv"
            outside.write_bytes(target.read_bytes())
            target.unlink()
            target.symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "symlink"):
                module.open_native_generation(context.manifest_path)

    def test_open_rejects_extra_and_missing_files(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with self.subTest(case="extra"):
            with tempfile.TemporaryDirectory() as tmpdir:
                _, context = _create_generation(module, tmpdir)
                _make_generation_writable(context.root_dir)
                (context.root_dir / "extra.txt").write_text(
                    "extra",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "unexpected entries"):
                    module.open_native_generation(context.manifest_path)

        with self.subTest(case="missing"):
            with tempfile.TemporaryDirectory() as tmpdir:
                _, context = _create_generation(module, tmpdir)
                _make_generation_writable(context.root_dir)
                (context.root_dir / "api_wind_daily.csv").unlink()
                with self.assertRaisesRegex(ValueError, "missing entries"):
                    module.open_native_generation(context.manifest_path)

    def test_open_parses_csv_from_the_verified_bytes(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            _, context = _create_generation(module, tmpdir)
            original = module.pd.read_csv
            inputs: list[object] = []

            def tracking_read_csv(source: object, *args: object, **kwargs: object):
                inputs.append(source)
                return original(source, *args, **kwargs)

            with patch.object(
                module.pd,
                "read_csv",
                side_effect=tracking_read_csv,
            ):
                module.open_native_generation(context.manifest_path)

            self.assertTrue(inputs)
            self.assertTrue(all(hasattr(item, "read") for item in inputs))
            self.assertFalse(any(isinstance(item, (str, os.PathLike)) for item in inputs))


class NativeInputGenerationDurabilityAndRetentionTests(unittest.TestCase):
    def test_atomic_publish_renames_a_complete_generation_once(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        publication_count = 0
        real_replace = os.replace

        def track_replace(source: object, destination: object) -> None:
            nonlocal publication_count
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                source_path.name.startswith(".building-")
                and destination_path.name.startswith("native-")
            ):
                publication_count += 1
                self.assertTrue(
                    (source_path / "manifest.json").is_file(),
                    "Native final directory was published without manifest",
                )
                module.open_native_generation(
                    source_path / "manifest.json",
                    expected_generation_id=destination_path.name,
                    expected_business_date="2026-07-24",
                    expected_feature_date="2026-07-24",
                )
            real_replace(source, destination)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                module.os,
                "replace",
                side_effect=track_replace,
            ):
                _, context = _create_generation(module, tmpdir)

            self.assertEqual(publication_count, 1)
            self.assertTrue(context.manifest_path.is_file())

    def test_publish_renames_once_when_platform_allows_sealed_rename(
        self,
    ) -> None:
        """平台允许重命名只读目录时保持发布前封存，且只 rename 一次。"""
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir)
            if not module._sealed_rename_supported(parent):
                self.skipTest(
                    "该平台不允许重命名只读目录，发布前封存路径不可用"
                )
            try:
                staging = parent / ".building-x"
                staging.mkdir()
                (staging / "manifest.json").write_text("{}", encoding="utf-8")
                os.chmod(staging / "manifest.json", 0o444)
                destination = parent / "native-abc"
                renames: list[tuple[str, str]] = []
                real_replace = os.replace

                def counting(source: object, target: object) -> None:
                    renames.append((Path(source).name, Path(target).name))
                    real_replace(source, target)

                with patch.object(module.os, "replace", side_effect=counting):
                    module._publish_sealed_generation(staging, destination)

                self.assertEqual(len(renames), 1)
                self.assertEqual(
                    stat.S_IMODE(os.stat(destination).st_mode), 0o555
                )
            finally:
                for path in parent.iterdir():
                    os.chmod(path, 0o755)

    def test_publish_withdraws_generation_when_sealing_fails(self) -> None:
        """降级路径上封存任一步失败都不得留下可见且可写的 destination。"""
        module = importlib.import_module("shared.native_input_generation")
        for name, error in (
            ("fchmod", PermissionError(13, "denied")),
            ("fsync", OSError(5, "io error")),
        ):
            with self.subTest(step=name), tempfile.TemporaryDirectory() as td:
                parent = Path(td)
                module._SEALED_RENAME_SUPPORT[str(parent)] = False
                try:
                    staging = parent / ".building-x"
                    staging.mkdir()
                    (staging / "manifest.json").write_text(
                        "{}", encoding="utf-8"
                    )
                    destination = parent / "native-abc"
                    with patch.object(module.os, name, side_effect=error):
                        with self.assertRaises(type(error)):
                            module._publish_sealed_generation(
                                staging, destination
                            )
                    self.assertFalse(
                        os.path.lexists(destination),
                        "封存失败后仍留下已发布的 generation",
                    )
                    self.assertFalse(os.path.lexists(staging))
                finally:
                    module._SEALED_RENAME_SUPPORT.pop(str(parent), None)

    def test_publish_withdraws_generation_when_mode_check_fails(self) -> None:
        """模式复核不通过同样必须撤回发布。"""
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir)
            module._SEALED_RENAME_SUPPORT[str(parent)] = False
            try:
                staging = parent / ".building-x"
                staging.mkdir()
                (staging / "manifest.json").write_text("{}", encoding="utf-8")
                destination = parent / "native-abc"
                with patch.object(module.os, "fchmod", return_value=None):
                    with self.assertRaises(RuntimeError):
                        module._publish_sealed_generation(staging, destination)
                self.assertFalse(
                    os.path.lexists(destination),
                    "模式复核失败后仍留下已发布的 generation",
                )
            finally:
                module._SEALED_RENAME_SUPPORT.pop(str(parent), None)

    def test_retention_rename_restores_sealed_mode_when_retry_fails(
        self,
    ) -> None:
        """GC 放宽写位后重试仍失败时，必须恢复 0o555 再抛出。"""
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir)
            source = parent / "native-abc"
            source.mkdir()
            os.chmod(source, 0o555)
            target = parent / ".gc-native-abc"
            with patch.object(
                module.os, "rename", side_effect=OSError(5, "io error")
            ):
                with self.assertRaises(OSError):
                    module._rename_with_temporarily_writable_source(
                        source, target
                    )
            self.assertEqual(
                stat.S_IMODE(os.lstat(source).st_mode),
                0o555,
                "重试失败后未恢复封存模式",
            )
            self.assertFalse(os.path.lexists(target))
            os.chmod(source, 0o755)

    def test_post_rename_failure_preserves_complete_final_generation(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        real_replace = os.replace
        real_open = module.open_native_generation
        published = False

        def track_replace(source: object, destination: object) -> None:
            nonlocal published
            source_path = Path(source)
            destination_path = Path(destination)
            real_replace(source, destination)
            if (
                source_path.name.startswith(".building-")
                and destination_path.name.startswith("native-")
            ):
                published = True

        def fail_final_reopen(path: Path, **kwargs: object):
            if (
                published
                and Path(path).parent.name.startswith("native-")
            ):
                raise OSError("injected post-rename reopen failure")
            return real_open(path, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir)
            with (
                patch.object(
                    module.os,
                    "replace",
                    side_effect=track_replace,
                ),
                patch.object(
                    module,
                    "open_native_generation",
                    side_effect=fail_final_reopen,
                ),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "post-rename reopen failure",
                ):
                    _create_generation(module, output_root)

            finals = [
                entry
                for entry in output_root.iterdir()
                if entry.name.startswith("native-")
            ]
            self.assertEqual(len(finals), 1)
            reopened = module.open_native_generation(
                finals[0] / "manifest.json",
                expected_generation_id=finals[0].name,
                expected_business_date="2026-07-24",
                expected_feature_date="2026-07-24",
            )
            self.assertEqual(reopened.generation_id, finals[0].name)

    def test_post_rename_root_fsync_failure_is_not_reported_as_success(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        real_replace = os.replace
        real_fsync_directory = module._fsync_directory
        published = False

        def track_replace(source: object, destination: object) -> None:
            nonlocal published
            real_replace(source, destination)
            if (
                Path(source).name.startswith(".building-")
                and Path(destination).name.startswith("native-")
            ):
                published = True

        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir)

            def fail_root_fsync(path: Path) -> None:
                if published and Path(path) == output_root:
                    raise OSError("injected root fsync failure")
                real_fsync_directory(path)

            with (
                patch.object(
                    module.os,
                    "replace",
                    side_effect=track_replace,
                ),
                patch.object(
                    module,
                    "_fsync_directory",
                    side_effect=fail_root_fsync,
                ),
                self.assertRaisesRegex(
                    OSError,
                    "root fsync failure",
                ),
            ):
                _create_generation(module, output_root)

            finals = [
                entry
                for entry in output_root.iterdir()
                if entry.name.startswith("native-")
            ]
            self.assertEqual(len(finals), 1)
            module.open_native_generation(
                finals[0] / "manifest.json",
                expected_generation_id=finals[0].name,
            )

    def test_publish_fsyncs_all_files_and_directories_around_atomic_rename(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        events: list[str] = []
        real_replace = os.replace

        def track_fsync(_descriptor: int) -> None:
            events.append("fsync")

        def track_replace(source: object, destination: object) -> None:
            events.append("replace")
            real_replace(source, destination)

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(module.os, "fsync", side_effect=track_fsync),
                patch.object(module.os, "replace", side_effect=track_replace),
            ):
                _, context = _create_generation(module, tmpdir)

            self.assertTrue(context.manifest_path.is_file())
            replace_index = events.index("replace")
            self.assertGreaterEqual(
                events[:replace_index].count("fsync"),
                len(module.NATIVE_GENERATION_FILENAMES) + 2,
            )
            self.assertIn("fsync", events[replace_index + 1 :])

    def test_legacy_caller_supplied_retention_is_disabled(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        pruner = getattr(module, "prune_native_generations", None)
        self.assertIsNotNone(pruner)
        if pruner is None:
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            _frames, context = _create_generation(module, tmpdir)
            with self.assertRaisesRegex(RuntimeError, "DB-resolved"):
                pruner(
                    tmpdir,
                    bound_generation_ids=set(),
                    min_free_bytes=0,
                )
            self.assertTrue(context.root_dir.is_dir())

    def test_db_resolved_exact_delete_rehashes_before_removal(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        deleter = getattr(
            module,
            "delete_reclaimable_native_generation",
            None,
        )
        self.assertIsNotNone(deleter)
        if deleter is None:
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            _frames, context = _create_generation(module, tmpdir)
            with self.assertRaisesRegex(ValueError, "manifest .* mismatch"):
                deleter(
                    tmpdir,
                    generation_id=context.generation_id,
                    manifest_sha256="f" * 64,
                    business_date=context.business_date,
                    feature_date=context.feature_date,
                )
            self.assertTrue(context.root_dir.is_dir())

            self.assertTrue(
                deleter(
                    tmpdir,
                    generation_id=context.generation_id,
                    manifest_sha256=context.manifest_sha256,
                    business_date=context.business_date,
                    feature_date=context.feature_date,
                )
            )
            self.assertFalse(context.root_dir.exists())
            self.assertFalse(
                deleter(
                    tmpdir,
                    generation_id=context.generation_id,
                    manifest_sha256=context.manifest_sha256,
                    business_date=context.business_date,
                    feature_date=context.feature_date,
                )
            )

    def test_storage_preflight_enforces_total_quota_and_free_watermark(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        preflight = getattr(
            module,
            "preflight_native_generation_storage",
            None,
        )
        self.assertIsNotNone(preflight)
        if preflight is None:
            return
        self.assertIn(
            "reserve_bytes",
            inspect.signature(preflight).parameters,
        )
        self.assertIn(
            "reserve_generation_count",
            inspect.signature(preflight).parameters,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            _frames, context = _create_generation(module, tmpdir)
            with self.assertRaisesRegex(OSError, "quota"):
                preflight(
                    tmpdir,
                    min_free_bytes=0,
                    max_total_bytes=1,
                    max_generation_count=1,
                )
            with self.assertRaisesRegex(OSError, "generation count"):
                preflight(
                    tmpdir,
                    min_free_bytes=0,
                    max_total_bytes=10**9,
                    max_generation_count=0,
                )
            with (
                patch.object(
                    module.shutil,
                    "disk_usage",
                    return_value=shutil._ntuple_diskusage(
                        total=100,
                        used=99,
                        free=1,
                    ),
                ),
                self.assertRaisesRegex(OSError, "watermark"),
            ):
                preflight(
                    tmpdir,
                    min_free_bytes=2,
                    max_total_bytes=10**9,
                    max_generation_count=1,
                )
            result = preflight(
                tmpdir,
                min_free_bytes=0,
                max_total_bytes=10**9,
                max_generation_count=1,
            )
            self.assertGreaterEqual(result["total_bytes"], 1)
            self.assertEqual(result["generation_count"], 1)
            self.assertTrue(context.root_dir.is_dir())
            with self.assertRaisesRegex(OSError, "quota"):
                preflight(
                    tmpdir,
                    min_free_bytes=0,
                    max_total_bytes=result["total_bytes"],
                    max_generation_count=2,
                    reserve_bytes=1,
                )
            with self.assertRaisesRegex(OSError, "generation count"):
                preflight(
                    tmpdir,
                    min_free_bytes=0,
                    max_total_bytes=10**9,
                    max_generation_count=1,
                    reserve_generation_count=1,
                )

    def test_create_checks_actual_staging_bytes_before_publish(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        signature = inspect.signature(module.create_native_generation)
        self.assertIn("max_total_bytes", signature.parameters)
        self.assertIn("min_free_bytes", signature.parameters)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaisesRegex(OSError, "quota"):
                _create_generation(
                    module,
                    root,
                    max_total_bytes=1,
                    min_free_bytes=0,
                )
            self.assertEqual(list(root.glob("native-*")), [])
            self.assertEqual(list(root.glob(".building-*")), [])

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with (
                patch.object(
                    module.shutil,
                    "disk_usage",
                    return_value=shutil._ntuple_diskusage(
                        total=100,
                        used=100,
                        free=0,
                    ),
                ),
                self.assertRaisesRegex(OSError, "watermark"),
            ):
                _create_generation(
                    module,
                    root,
                    max_total_bytes=10**9,
                    min_free_bytes=1,
                )
            self.assertEqual(list(root.glob("native-*")), [])
            self.assertEqual(list(root.glob(".building-*")), [])

    def test_create_existing_content_addressed_final_is_not_double_counted(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _engine, first = _create_generation(module, root)
            used_bytes = sum(
                path.stat().st_size
                for path in root.rglob("*")
                if path.is_file()
            )

            _engine, second = _create_generation(
                module,
                root,
                max_total_bytes=used_bytes,
                min_free_bytes=0,
            )

            self.assertEqual(second.generation_id, first.generation_id)
            self.assertEqual(
                [path.name for path in root.glob("native-*")],
                [first.generation_id],
            )
            self.assertEqual(list(root.glob(".building-*")), [])


class NativeInputGenerationRecoveryScanTests(unittest.TestCase):
    def test_create_makes_new_storage_root_private_and_open_requires_it(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir) / "nested" / "native-generations"
            _frames, context = _create_generation(module, output_root)
            self.assertEqual(
                stat.S_IMODE(output_root.lstat().st_mode),
                0o700,
            )
            output_root.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "private"):
                module.open_native_generation(context.manifest_path)

    def test_create_rejects_existing_nonprivate_storage_root(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir) / "native-generations"
            output_root.mkdir(mode=0o755)
            with self.assertRaisesRegex(ValueError, "private"):
                _create_generation(module, output_root)

    def test_owner_cleanup_removes_building_and_gc_debris(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        cleaner = getattr(module, "cleanup_native_generation_debris", None)
        self.assertIsNotNone(cleaner)
        if cleaner is None:
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            building = root / ".building-interrupted"
            building.mkdir()
            (building / "partial.csv").write_text(
                "partial",
                encoding="utf-8",
            )
            tombstone = root / (
                f".gc-native-{'a' * 24}-{'b' * 32}"
            )
            tombstone.mkdir()
            (tombstone / "manifest.json").write_text(
                "{}",
                encoding="utf-8",
            )

            removed = cleaner(root)

            self.assertEqual(
                removed,
                (building.name, tombstone.name),
            )
            self.assertFalse(building.exists())
            self.assertFalse(tombstone.exists())

    def test_owner_cleanup_rejects_symlink_and_non_private_root(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        cleaner = getattr(module, "cleanup_native_generation_debris", None)
        self.assertIsNotNone(cleaner)
        if cleaner is None:
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outside = root / "outside"
            outside.mkdir()
            unsafe = root / ".building-symlink"
            unsafe.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                cleaner(root)
            self.assertTrue(outside.is_dir())

            unsafe.unlink()
            root.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "private"):
                cleaner(root)

    def test_scan_ignores_partial_staging_and_returns_zero_or_one(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        scanner = getattr(module, "find_published_native_generation", None)
        self.assertIsNotNone(scanner)
        if scanner is None:
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            partial = root / ".building-interrupted"
            partial.mkdir()
            (partial / "payload.tmp").write_text(
                "partial",
                encoding="utf-8",
            )

            self.assertIsNone(
                scanner(
                    root,
                    business_date="2026-07-24",
                    feature_date="2026-07-24",
                )
            )
            _, created = _create_generation(module, root)
            recovered = scanner(
                root,
                business_date="2026-07-24",
                feature_date="2026-07-24",
            )

            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.generation_id, created.generation_id)
            self.assertEqual(
                recovered.manifest_sha256,
                created.manifest_sha256,
            )

    def test_scan_rejects_ambiguous_same_day_generations(self) -> None:
        module = importlib.import_module("shared.native_input_generation")
        scanner = getattr(module, "find_published_native_generation", None)
        self.assertIsNotNone(scanner)
        if scanner is None:
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            _create_generation(
                module,
                tmpdir,
                source_commit_token="a" * 64,
            )
            _create_generation(
                module,
                tmpdir,
                source_commit_token="b" * 64,
            )

            with self.assertRaisesRegex(ValueError, "ambiguous"):
                scanner(
                    tmpdir,
                    business_date="2026-07-24",
                    feature_date="2026-07-24",
                )

    def test_scan_rejects_final_looking_directory_without_manifest(
        self,
    ) -> None:
        module = importlib.import_module("shared.native_input_generation")
        scanner = getattr(module, "find_published_native_generation", None)
        self.assertIsNotNone(scanner)
        if scanner is None:
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            invalid = Path(tmpdir) / f"native-{'a' * 24}"
            invalid.mkdir()
            (invalid / "payload.csv").write_text(
                "value\n1\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "manifest"):
                scanner(
                    tmpdir,
                    business_date="2026-07-24",
                    feature_date="2026-07-24",
                )


if __name__ == "__main__":
    unittest.main()
