"""多目标交付的版本、完整执行和一次持久化公共边界。"""

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import yaml

from scheduler.discovery import blackbox_deliveries, load_scheme_config
from shared.blackbox_v2.contracts import BlackboxRequest
from shared.blackbox_v2.history import HistoricalCase
from shared.blackbox_v2.snapshot import BlackboxSnapshot, CutoffKeys
from shared.models import PredictionRecord
from shared.scheme_config_schema import MULTI_TARGET_DELIVERIES, validate_config


def _scheme(root, scheme_id="t1_daily"):
    directory = root / scheme_id
    (directory / "delivery").mkdir(parents=True)
    declarations = []
    for tenor, basename in MULTI_TARGET_DELIVERIES[scheme_id].items():
        declarations.append({"target_tenor": tenor, "script": f"delivery/{basename}.py",
                             "metadata": f"delivery/{basename}.json"})
        (directory / "delivery" / f"{basename}.py").write_text("# immutable algorithm fixture\n")
        metadata = {"schema_version": "1.0", "scheme_id": scheme_id, "name": f"Trial {tenor}",
                    "algorithm_version": "1.0.0", "target_tenor": tenor,
                    "task_type": "T+1" if scheme_id == "t1_daily" else "T+5",
                    "horizon": 1 if scheme_id == "t1_daily" else 5,
                    "target_rule": "target_date_yield_vs_feature_date_yield",
                    "description": "Fixture", "owner": "test"}
        (directory / "delivery" / f"{basename}.json").write_text(json.dumps(metadata))
    raw = {"scheme_id": scheme_id, "runtime_type": "blackbox_v2", "input_source": "data_bridge_current",
           "runtime_profile": "blackbox-v2-v1", "data_schema_version": "data-bridge-v1",
           "factor_input_mode": "algorithm_managed", "status": "paused", "version_status": "draft",
           "schedule": {"cron": "3 7 * * 1-5", "timezone": "Asia/Shanghai", "timeout_sec": 120},
           "deliveries": declarations}
    config = directory / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    return config, raw


def _snapshot():
    return BlackboxSnapshot(snapshot_id="snapshot", root_dir=Path("/sealed"),
                            data_dir=Path("/sealed/data"), manifest_path=Path("/sealed/manifest.json"),
                            schema_version="data-bridge-v1", generation_id="generation", refresh_date="2026-09-11")


def _request(metadata):
    return BlackboxRequest("same-base-request", "2026-09-11", "2026-09-10",
                           "2026-09-11", "2026-09-10", "202636", "202608")


def _record(metadata, request):
    return PredictionRecord(scheme_id=metadata.scheme_id, target_tenor=metadata.target_tenor,
                            horizon=metadata.horizon, predict_date=request.predict_date,
                            feature_date=request.feature_date, target_date=request.target_date,
                            predicted_direction=-1,
                            extra={"request_id": request.request_id, "data_snapshot_id": "snapshot"})


@contextmanager
def _runtime_view(bundle):
    yield SimpleNamespace(bundle=bundle, data_dir=Path("/private/data"))


@pytest.mark.parametrize("scheme_id", list(MULTI_TARGET_DELIVERIES))
def test_complete_target_set_has_one_version_and_preserves_basename(tmp_path, scheme_id):
    path, raw = _scheme(tmp_path, scheme_id)
    cfg = load_scheme_config(path)
    assert cfg.delivery_script is cfg.delivery_metadata is cfg.blackbox_metadata is None
    assert cfg.tenors == list(MULTI_TARGET_DELIVERIES[scheme_id])
    assert {item.metadata.scheme_id for item in blackbox_deliveries(cfg)} == {scheme_id}
    assert {item.script_path.stem for item in blackbox_deliveries(cfg)} == set(MULTI_TARGET_DELIVERIES[scheme_id].values())
    raw["deliveries"].reverse()
    path.write_text(yaml.safe_dump(raw))
    assert load_scheme_config(path).scheme_version == cfg.scheme_version
    for item in blackbox_deliveries(cfg):
        for member in (item.script_path, item.metadata_path):
            original = member.read_bytes()
            member.write_bytes(original + b"\n")
            changed = load_scheme_config(path)
            assert changed.scheme_version != cfg.scheme_version
            assert changed.config_hash == cfg.config_hash
            member.write_bytes(original)


@pytest.mark.parametrize("mutation", ["other_id", "native", "missing", "duplicate", "path", "extra_field", "state", "single"])
def test_multi_target_mapping_is_closed(tmp_path, mutation):
    _, raw = _scheme(tmp_path)
    if mutation == "other_id":
        raw["scheme_id"] = "new_trial"
    elif mutation == "native":
        raw["runtime_type"] = "native_adapter"
    elif mutation == "missing":
        raw["deliveries"].pop()
    elif mutation == "duplicate":
        raw["deliveries"][1] = dict(raw["deliveries"][0])
    elif mutation == "path":
        raw["deliveries"][0]["script"] = "../source.py"
    elif mutation == "extra_field":
        raw["deliveries"][0]["fallback"] = "native"
    elif mutation == "state":
        raw["incremental_state"] = True
    else:
        raw["delivery"] = {"script": "one.py", "metadata": "one.json"}
    assert validate_config(raw, raw["scheme_id"])


@pytest.mark.parametrize("mutation", ["tenor", "identity", "task", "version", "owner", "extra_file", "symlink"])
def test_discovery_rejects_partial_or_conflicting_target_packages(tmp_path, mutation):
    path, _ = _scheme(tmp_path)
    metadata_path = path.parent / "delivery/t1_daily_10y_bbv2.json"
    if mutation == "extra_file":
        (metadata_path.parent / "extra.py").write_text("pass\n")
    elif mutation == "symlink":
        original = metadata_path.read_bytes()
        metadata_path.unlink()
        other = tmp_path / "outside.json"
        other.write_bytes(original)
        metadata_path.symlink_to(other)
    else:
        metadata = json.loads(metadata_path.read_text())
        if mutation == "tenor":
            metadata["target_tenor"] = "5Y"
        elif mutation == "identity":
            metadata["scheme_id"] = "t1_daily_10y_bbv2"
        elif mutation == "task":
            metadata.update(task_type="T+5", horizon=5)
        elif mutation == "version":
            metadata["algorithm_version"] = "2.0.0"
        else:
            metadata["owner"] = "other"
        metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError):
        load_scheme_config(path)


@pytest.mark.parametrize("failure", [False, True])
def test_live_targets_share_input_and_fail_as_one_batch(tmp_path, failure):
    from scheduler.executor import run_blackbox_scheme_subprocess
    from shared.calendar_service import CalendarService

    cfg = load_scheme_config(_scheme(tmp_path)[0])
    calendar = CalendarService(None)
    calendar.__dict__["_trading_days"] = ["2026-09-10", "2026-09-11"]
    seen = []

    def predict(**kwargs):
        seen.append(kwargs)
        if failure and len(seen) == 2:
            raise ValueError("invalid second Result")
        return _record(kwargs["metadata"], kwargs["request"])

    with (
        patch("scheduler.executor.get_ready_blackbox_snapshot", return_value=_snapshot()) as snapshot,
        patch("scheduler.executor.get_calendar", return_value=calendar),
        patch("scheduler.executor.resolve_blackbox_input_cutoffs", return_value=CutoffKeys("2026-09-10", "202636", "202608")),
        patch("scheduler.executor.open_blackbox_runtime_view", side_effect=_runtime_view) as view,
        patch("scheduler.blackbox_v2_runner.run_blackbox_predict", side_effect=predict),
    ):
        if failure:
            with pytest.raises(ValueError, match="second Result"):
                run_blackbox_scheme_subprocess(cfg, "2026-09-11", engine=object(), algo_env="forecast_env", timeout_sec=120)
        else:
            records = run_blackbox_scheme_subprocess(cfg, "2026-09-11", engine=object(), algo_env="forecast_env", timeout_sec=120)
            assert [record.target_tenor for record in records] == cfg.tenors
        assert snapshot.call_count == view.call_count == 1
    assert len(seen) == 2
    assert len({item["request"].request_id for item in seen}) == 1
    assert seen[0]["request"].request_id == "t1_daily:2026-09-11:2026-09-10:2026-09-11"
    assert len({item["data_snapshot_id"] for item in seen}) == 1
    assert len({item["data_dir"] for item in seen}) == 1
    assert all("state" not in item for item in seen)
    assert 0 < seen[1]["timeout_sec"] <= seen[0]["timeout_sec"] <= 120


def test_retained_native_helper_is_scoped_to_exact_migration_identity():
    from shared.scheme_config_schema import validate_native_attachments

    validate_native_attachments("t5_daily", {"latest_prediction.py": "a" * 64})
    with pytest.raises(ValueError, match="outside"):
        validate_native_attachments("t1_daily", {"latest_prediction.py": "a" * 64})


@pytest.mark.parametrize("failure", [None, "invalid_result", "process", "source_drift"])
def test_backtest_validates_all_targets_before_one_atomic_persist(tmp_path, failure):
    from harness.blackbox_v2.gates import BlackboxBacktestGate
    from harness.context import GateContext
    from harness.result import GateStatus

    path, _ = _scheme(tmp_path)
    cfg = load_scheme_config(path)
    calls = []

    def run_delivery(**kwargs):
        from scheduler.blackbox_v2_runner import _validate_runtime_profile

        _validate_runtime_profile(kwargs["profile"], source="multi-target test")
        calls.append(kwargs)
        if len(calls) == 2 and failure == "process":
            raise RuntimeError("second target failed")
        records = [_record(kwargs["metadata"], request) for request in kwargs["requests"]]
        if len(calls) == 2 and failure == "invalid_result":
            records[0] = replace(records[0], predicted_direction=7)
        if len(calls) == 2 and failure == "source_drift":
            cfg.blackbox_deliveries[0].script_path.write_text("# changed after execution\n")
        return records

    context = GateContext(cfg.scheme_id, "2026-09-12", tmp_path, config=cfg, persist_backtest=True,
                          engine_factory=Mock, timeout_sec=120)
    with (
        patch("harness.blackbox_v2.gates.verify_direct_operation", return_value=(SimpleNamespace(issued_by="operator"), [])),
        patch("harness.blackbox_v2.gates.operation_scope_sha256", return_value="scope"),
        patch("harness.blackbox_v2.gates.get_ready_blackbox_snapshot", return_value=_snapshot()),
        patch("harness.blackbox_v2.gates.build_historical_cases", side_effect=lambda metadata, *a, **kw: [HistoricalCase(_request(metadata), -1, {})]),
        patch("harness.blackbox_v2.gates._environment_fingerprint", return_value="environment"),
        patch("harness.blackbox_v2.gates._open_runtime_input", side_effect=_runtime_view) as view,
        patch("harness.blackbox_v2.gates.run_blackbox_backtest", side_effect=run_delivery),
        patch("harness.blackbox_v2.gates.persist_backtest_output_atomic", return_value=10) as persist,
    ):
        result = BlackboxBacktestGate().run(context)
    assert len(calls) == 2
    assert view.call_count == 1
    if failure:
        assert result.status == GateStatus.FAILED
        persist.assert_not_called()
    else:
        assert result.status == GateStatus.PASSED, result.errors
        persist.assert_called_once()
        output = persist.call_args.args[1]
        assert {row["target_tenor"] for row in output.rows} == set(cfg.tenors)
        assert output.summary["request_count"] == output.summary["row_count"] == 2
        assert len(output.monthly_metrics) == 2
        assert {row["extra"]["scheme_version"] for row in output.rows} == {cfg.scheme_version}
