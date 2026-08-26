from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


class _Engine:
    def __init__(self) -> None:
        self.connection = SimpleNamespace()
        self.begin_count = 0

    @contextmanager
    def begin(self):
        self.begin_count += 1
        yield self.connection


def _item(run_id: int, predict_date: str, target_date: str) -> dict[str, object]:
    return {
        "cfg": SimpleNamespace(
            scheme_id="demo_blackbox",
            scheme_version="version-1",
            runtime_type="blackbox_v2",
        ),
        "run_id": run_id,
        "records": [SimpleNamespace()],
        "expected_target_keys": [
            {
                "base_scheme_id": "demo_blackbox",
                "target_tenor": "5Y",
                "horizon": 1,
                "predict_date": predict_date,
                "feature_date": predict_date,
                "target_date": target_date,
            }
        ],
        "source_authority": None,
        "records_returned": 1,
        "run_date": predict_date,
        "duration_sec": 1.0,
    }


def test_range_checks_every_business_key_before_any_prediction_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scheduler import repository

    engine = _Engine()
    insert = Mock(return_value=1)
    absent = Mock(side_effect=RuntimeError("business key conflict"))
    monkeypatch.setattr(
        repository,
        "_normalize_gray_gap_target_keys",
        lambda _cfg, targets: targets,
    )
    monkeypatch.setattr(repository, "_normalize_gray_gap_source_authority", lambda *_a, **_k: None)
    monkeypatch.setattr(repository, "_validate_gray_gap_records", lambda *_a, **_k: None)
    monkeypatch.setattr(
        repository,
        "_enrich_gray_gap_records",
        lambda _cfg, *, records, **_k: records,
    )
    monkeypatch.setattr(repository, "_blackbox_gray_gap_snapshot_id", lambda *_a, **_k: None)
    monkeypatch.setattr(repository, "_read_scheme_run_conn", lambda *_a, **_k: {})
    monkeypatch.setattr(repository, "_validate_gray_gap_run", lambda *_a, **_k: None)
    monkeypatch.setattr(repository, "_validate_gray_gap_active_version", lambda *_a, **_k: None)
    monkeypatch.setattr(repository, "_validate_gray_gap_active_registry", lambda *_a, **_k: None)
    monkeypatch.setattr(repository, "_assert_gray_gap_business_keys_absent", absent)
    monkeypatch.setattr(repository, "_insert_run_predictions_conn", insert)

    with pytest.raises(RuntimeError, match="business key conflict"):
        repository.complete_gray_gap_runs_atomic(
            engine,
            [
                _item(101, "2026-05-30", "2026-06-05"),
                _item(102, "2026-06-06", "2026-06-12"),
            ],
        )

    assert engine.begin_count == 1
    absent.assert_called_once()
    assert len(absent.call_args.args[1]) == 2
    insert.assert_not_called()
