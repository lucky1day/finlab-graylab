"""Dashboard 公网 benchmark 的 schema 回归。"""

from __future__ import annotations

import copy

import pytest

from scripts.benchmark_factor_lab_dashboard import validate_dashboard_schema
from tests.factor_lab_dashboard_conformance import dashboard_v1_conformance_samples


def _canonical_payload() -> dict:
    return next(
        sample["payload"]
        for sample in dashboard_v1_conformance_samples()
        if sample["name"] == "canonical"
    )


def test_benchmark_accepts_current_live_signal_fields() -> None:
    """正式 probe 必须接受 Dashboard 当前的 signal 状态字段。"""
    validate_dashboard_schema(_canonical_payload())


def test_benchmark_rejects_missing_signal_without_failure_category() -> None:
    payload = copy.deepcopy(_canonical_payload())
    payload["schemes"][0]["signal_status"] = "missing"
    payload["schemes"][0]["signal_failure_category"] = None

    with pytest.raises(ValueError, match="signal_failure_category"):
        validate_dashboard_schema(payload)
