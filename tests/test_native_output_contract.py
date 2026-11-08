from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from shared import daily_0629_predict_adapter as daily
from shared import monthly_predict_adapter as monthly
from shared import weekly_average_lgbm_predict_adapter as weekly


@pytest.fixture(params=["daily", "weekly", "monthly"])
def adapter(request):
    """通过实际适配入口校验源输出，隔离数据库及原始算法执行。"""
    kind = request.param
    module = {"daily": daily, "weekly": weekly, "monthly": monthly}[kind]
    frequency = {"daily": "D1Y", "weekly": "W1Y", "monthly": "M1Y"}[kind]
    evidence = SimpleNamespace(
        frequency=frequency, target_tenor="1Y", model_id="frozen-model",
        source_package_hash="frozen-source", final_select_id="frozen-selection",
        target_column="TB1YWI0C",
    )
    source = {"frequency": frequency, "effective_week_id": 202645}
    context = SimpleNamespace(
        db_rdate="2026-11-15", trigger_date="2026-11-15",
        scheduled_trigger_date="2026-11-15", feature_date="2026-11-13",
        target_date="2026-12-15", feature_month_id="2026-11",
        target_month_id="2026-12",
    )
    calendar = Mock()
    calendar.week_id_to_last_trading_day.side_effect = lambda key: (
        "2026-11-06" if key == 202645 else "2026-11-13"
    )
    with ExitStack() as stack:
        for name, value in (
            ("load_source_runtime_database_config", object()),
            ("create_input_engine", Mock()), ("get_calendar", calendar),
            ({"daily": "require_daily_0629_source_evidence",
              "weekly": "require_weekly_average_source_evidence",
              "monthly": "require_monthly_source_evidence"}[kind], evidence),
            ({"daily": "run_source_daily_live", "weekly": "run_source_weekly_live",
              "monthly": "run_source_monthly_live"}[kind], [source]),
            (f"build_{kind}_input_artifact", SimpleNamespace(path="/test/input.csv", source="test")),
        ):
            stack.enter_context(patch.object(module, name, return_value=value))
        if kind == "weekly":
            stack.enter_context(patch.object(module, "next_calendar_week_id", return_value=202646))
        else:
            stack.enter_context(patch.object(module, f"build_{kind}_live_context", return_value=context))
        run = {"daily": daily.run_daily_0629_prediction,
               "weekly": weekly.run_weekly_average_lgbm_prediction,
               "monthly": monthly.run_monthly_prediction}[kind]
        yield SimpleNamespace(
            kind=kind, source=source, field="y_pred" if kind == "monthly" else "pred_label",
            run=lambda: run("frozen_scheme", "2026-11-15"),
        )


@pytest.mark.parametrize("value", [None, "", "invalid", 0.9, "1.9", True, False, float("nan"), float("inf"), "-inf", 2])
def test_native_adapter_rejects_missing_or_illegal_direction(adapter, value):
    adapter.source[adapter.field] = value
    with pytest.raises(RuntimeError, match="direction|prediction|integer"):
        adapter.run()


@pytest.mark.parametrize("value", [0, 1, "0", "1", "0.0", "1.0", "1e0"])
def test_native_adapter_preserves_legal_csv_direction(adapter, value):
    adapter.source[adapter.field] = value
    record, = adapter.run()
    expected = int(float(value))
    if adapter.kind == "monthly" and expected == 0:
        expected = -1
    assert record.predicted_direction == expected


def test_native_direction_mapping_keeps_negative_direction(adapter):
    adapter.source[adapter.field] = "-1.0"
    if adapter.kind == "monthly":
        with pytest.raises(RuntimeError, match="direction"):
            adapter.run()
    else:
        assert adapter.run()[0].predicted_direction == -1


def test_daily_missing_final_direction_uses_existing_fallback(adapter):
    if adapter.kind != "daily":
        return
    # daily 的后备字段属于原适配合同，非法主字段不能借其掩盖。
    adapter.source["candidate_pred"] = "-1.0"
    assert adapter.run()[0].predicted_direction == -1
    adapter.source["pred_label"] = "invalid"
    with pytest.raises(RuntimeError, match="direction"):
        adapter.run()


def test_native_adapter_preserves_source_probability_audit(adapter):
    adapter.source[adapter.field] = "1.0"
    probability_field = "pred_proba_up" if adapter.kind == "monthly" else "prob_up"
    adapter.source[probability_field] = 0.625
    record, = adapter.run()
    assert record.extra[probability_field] == 0.625
