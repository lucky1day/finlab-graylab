"""DataBridge 日级 ready gate 只等待可恢复状态。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scheduler import one_shot_prediction_runner as runner
from scheduler.v2_daily_gate import (
    SCHEMA_VERSION,
    V2DailyGateBlocked,
    gate_record_path,
    load_gate_record,
    require_v2_daily_ready,
)
from shared.data_bridge.refresh import (
    DataBridgeCurrentInvalidError,
    DataBridgeCurrentMissingError,
)
from shared.data_bridge.validation import DataBridgeValidationError


RUN_DATE = "2026-09-16"
EXPECTED_DAILY_DATE = "2026-09-15"


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(runtime_root=tmp_path)


def _ready_record(**updates: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_date": RUN_DATE,
        "status": "ready",
        "checked_at": "2026-09-16T06:40:00+08:00",
        "generation_id": "generation-1",
        "refresh_date": RUN_DATE,
        "expected_daily_date": EXPECTED_DAILY_DATE,
        "business_digest": "a" * 64,
        "checks": [],
        "restart": {"requested": False, "verified": False},
    }
    record.update(updates)
    return record


def _write_record(tmp_path: Path, payload: object) -> None:
    path = gate_record_path(_config(tmp_path), RUN_DATE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _assert_blocked(
    action,
    *,
    code: str,
    retryable: bool,
) -> V2DailyGateBlocked:
    with pytest.raises(V2DailyGateBlocked) as caught:
        action()
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    return caught.value


def test_missing_gate_is_waitable_but_invalid_json_is_terminal(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _assert_blocked(
        lambda: load_gate_record(config, RUN_DATE),
        code="gate_not_ready",
        retryable=True,
    )

    path = gate_record_path(config, RUN_DATE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")
    _assert_blocked(
        lambda: load_gate_record(config, RUN_DATE),
        code="gate_record_invalid",
        retryable=False,
    )


def test_non_file_gate_path_is_terminal_damage(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = gate_record_path(config, RUN_DATE)
    path.mkdir(parents=True)
    _assert_blocked(
        lambda: load_gate_record(config, RUN_DATE),
        code="gate_record_invalid",
        retryable=False,
    )


@pytest.mark.parametrize(
    "updates, code, retryable",
    [
        ({"schema_version": "retired"}, "gate_schema_mismatch", False),
        ({"run_date": "2026-09-15"}, "gate_run_date_mismatch", False),
        ({"status": "checking"}, "gate_not_ready", True),
        ({"status": "blocked"}, "gate_blocked", False),
        (
            {"expected_daily_date": "2026-09-14"},
            "gate_expected_daily_date_mismatch",
            False,
        ),
    ],
)
def test_gate_record_contract_has_stable_failure_classification(
    tmp_path: Path,
    updates: dict[str, object],
    code: str,
    retryable: bool,
) -> None:
    _write_record(tmp_path, _ready_record(**updates))
    _assert_blocked(
        lambda: require_v2_daily_ready(
            _config(tmp_path), RUN_DATE, EXPECTED_DAILY_DATE
        ),
        code=code,
        retryable=retryable,
    )


def test_checking_gate_with_wrong_target_date_fails_immediately(
    tmp_path: Path,
) -> None:
    _write_record(
        tmp_path,
        _ready_record(
            status="checking",
            expected_daily_date="2026-09-14",
        ),
    )
    _assert_blocked(
        lambda: require_v2_daily_ready(
            _config(tmp_path), RUN_DATE, EXPECTED_DAILY_DATE
        ),
        code="gate_expected_daily_date_mismatch",
        retryable=False,
    )


@pytest.mark.parametrize("generation_id", (None, 7, ""))
def test_ready_gate_with_invalid_generation_identity_is_terminal(
    tmp_path: Path,
    generation_id: object,
) -> None:
    _write_record(tmp_path, _ready_record(generation_id=generation_id))
    _assert_blocked(
        lambda: require_v2_daily_ready(
            _config(tmp_path), RUN_DATE, EXPECTED_DAILY_DATE
        ),
        code="gate_record_invalid",
        retryable=False,
    )


@pytest.mark.parametrize(
    "failure, code, retryable",
    [
        (
            DataBridgeCurrentMissingError("switch in progress"),
            "gate_current_missing",
            True,
        ),
        (
            DataBridgeCurrentInvalidError("invalid current"),
            "gate_current_invalid",
            False,
        ),
        (
            DataBridgeValidationError("invalid schema"),
            "gate_current_invalid",
            False,
        ),
    ],
)
def test_current_dataset_failures_use_existing_exception_types(
    tmp_path: Path,
    failure: BaseException,
    code: str,
    retryable: bool,
) -> None:
    _write_record(tmp_path, _ready_record())
    with patch(
        "scheduler.v2_daily_gate.check_current_dataset",
        side_effect=failure,
    ):
        _assert_blocked(
            lambda: require_v2_daily_ready(
                _config(tmp_path), RUN_DATE, EXPECTED_DAILY_DATE
            ),
            code=code,
            retryable=retryable,
        )


def test_generation_mismatch_is_a_limited_reread_candidate(
    tmp_path: Path,
) -> None:
    _write_record(tmp_path, _ready_record())
    current = SimpleNamespace(
        state={
            "generation_id": "generation-2",
            "refresh_date": RUN_DATE,
            "business_digest": "b" * 64,
        }
    )
    with patch(
        "scheduler.v2_daily_gate.check_current_dataset",
        return_value=current,
    ):
        _assert_blocked(
            lambda: require_v2_daily_ready(
                _config(tmp_path), RUN_DATE, EXPECTED_DAILY_DATE
            ),
            code="gate_generation_mismatch",
            retryable=True,
        )


@pytest.mark.parametrize(
    "field, current_value, code",
    [
        ("refresh_date", "2026-09-15", "gate_refresh_date_mismatch"),
        ("business_digest", "b" * 64, "gate_business_digest_mismatch"),
    ],
)
def test_stable_current_identity_mismatch_is_terminal(
    tmp_path: Path,
    field: str,
    current_value: str,
    code: str,
) -> None:
    _write_record(tmp_path, _ready_record())
    current_state = {
        "generation_id": "generation-1",
        "refresh_date": RUN_DATE,
        "business_digest": "a" * 64,
    }
    current_state[field] = current_value
    with patch(
        "scheduler.v2_daily_gate.check_current_dataset",
        return_value=SimpleNamespace(state=current_state),
    ):
        _assert_blocked(
            lambda: require_v2_daily_ready(
                _config(tmp_path), RUN_DATE, EXPECTED_DAILY_DATE
            ),
            code=code,
            retryable=False,
        )


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def test_waiter_does_not_poll_terminal_contract_errors() -> None:
    clock = _Clock()
    failure = V2DailyGateBlocked(
        "schema mismatch",
        code="gate_schema_mismatch",
        retryable=False,
    )
    with patch.object(
        runner,
        "require_v2_daily_ready",
        side_effect=failure,
    ) as ready:
        code = runner._wait_for_v2_daily_ready(
            SimpleNamespace(),
            run_date=RUN_DATE,
            expected_daily_date=EXPECTED_DAILY_DATE,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert code == "gate_schema_mismatch"
    assert ready.call_count == 1
    assert clock.sleeps == []


@pytest.mark.parametrize(
    "transition_code",
    ["gate_generation_mismatch", "gate_current_missing"],
)
def test_publication_transition_gets_only_bounded_immediate_rereads(
    transition_code: str,
) -> None:
    clock = _Clock()
    failure = V2DailyGateBlocked(
        "publication transition",
        code=transition_code,
        retryable=True,
    )
    with patch.object(
        runner,
        "require_v2_daily_ready",
        side_effect=failure,
    ) as ready:
        code = runner._wait_for_v2_daily_ready(
            SimpleNamespace(),
            run_date=RUN_DATE,
            expected_daily_date=EXPECTED_DAILY_DATE,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            transition_retry_limit=3,
            transition_retry_interval_sec=0.1,
        )

    assert code == transition_code
    assert ready.call_count == 3
    assert clock.sleeps == [0.1, 0.1]


def test_publication_transition_can_resolve_on_bounded_reread() -> None:
    clock = _Clock()
    transient = V2DailyGateBlocked(
        "publication transition",
        code="gate_generation_mismatch",
        retryable=True,
    )
    with patch.object(
        runner,
        "require_v2_daily_ready",
        side_effect=[transient, {"status": "ready"}],
    ) as ready:
        code = runner._wait_for_v2_daily_ready(
            SimpleNamespace(),
            run_date=RUN_DATE,
            expected_daily_date=EXPECTED_DAILY_DATE,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            transition_retry_interval_sec=0.1,
        )

    assert code is None
    assert ready.call_count == 2
    assert clock.sleeps == [0.1]


def test_waitable_missing_gate_times_out_with_structured_progress() -> None:
    clock = _Clock()
    progress: list[dict[str, object]] = []
    failure = V2DailyGateBlocked(
        "not ready",
        code="gate_not_ready",
        retryable=True,
    )
    with patch.object(
        runner,
        "require_v2_daily_ready",
        side_effect=failure,
    ):
        code = runner._wait_for_v2_daily_ready(
            SimpleNamespace(),
            run_date=RUN_DATE,
            expected_daily_date=EXPECTED_DAILY_DATE,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            emit_progress=progress.append,
            max_wait_sec=90,
            poll_interval_sec=30,
            progress_interval_sec=30,
            warning_after_sec=60,
        )

    assert code == runner.SCHEDULED_PREFLIGHT_FAILURE_DATA_BRIDGE_READY_TIMEOUT
    assert clock.sleeps == [30, 30, 30]
    assert progress == [
        {
            "event": "v2_daily_gate_wait",
            "level": "info",
            "elapsed_sec": 0.0,
            "remaining_sec": 90.0,
            "code": "gate_not_ready",
        },
        {
            "event": "v2_daily_gate_wait",
            "level": "info",
            "elapsed_sec": 30.0,
            "remaining_sec": 60.0,
            "code": "gate_not_ready",
        },
        {
            "event": "v2_daily_gate_wait",
            "level": "warning",
            "elapsed_sec": 60.0,
            "remaining_sec": 30.0,
            "code": "gate_not_ready",
        },
        {
            "event": "v2_daily_gate_wait",
            "level": "warning",
            "elapsed_sec": 90.0,
            "remaining_sec": 0.0,
            "code": "gate_not_ready",
        },
    ]
