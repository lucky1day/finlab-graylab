"""Actual updater 只负责范围、共享事实构建和 Repository 写入的接线。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scheduler import daily_actuals_updater
from scheduler import monthly_actuals_updater
from scheduler import period_average_actuals_updater
from scheduler import weekly_actuals_updater


@pytest.mark.parametrize("injected", [False, True])
def test_daily_updater_passes_scope_and_cutoff_to_shared_builder(
    injected: bool,
) -> None:
    engine = MagicMock()
    rows = [{"tenor": "5Y"}]
    records = [object()]
    watermarks = {"5Y": "2026-08-25"}

    with (
        patch.object(
            daily_actuals_updater,
            "create_engine_from_env",
            return_value=engine,
        ) as create,
        patch.object(
            daily_actuals_updater,
            "resolve_actual_tenors",
            return_value=["5Y"],
        ) as resolve,
        patch.object(
            daily_actuals_updater,
            "read_yield_rows",
            return_value=rows,
        ) as read_rows,
        patch.object(
            daily_actuals_updater,
            "build_daily_actual_records_from_rows",
            return_value=records,
        ) as build,
        patch.object(
            daily_actuals_updater,
            "upsert_actuals",
            return_value=1,
        ) as write,
        patch.object(
            daily_actuals_updater,
            "read_source_watermarks",
            return_value=watermarks,
        ) as read_watermarks,
        patch.object(
            daily_actuals_updater,
            "delete_actuals_after_source_watermark",
            return_value=0,
        ) as prune,
    ):
        written = daily_actuals_updater.update_actuals(
            start_date="2026-08-01",
            end_date="2026-08-25",
            tenors=["5Y"],
            engine=engine if injected else None,
        )

    assert written == 1
    resolve.assert_called_once_with(engine, frequency="daily", tenors=["5Y"])
    read_rows.assert_called_once_with(
        engine,
        tenors=["5Y"],
        end_date="2026-08-25",
    )
    build.assert_called_once_with(rows, start_date="2026-08-01")
    write.assert_called_once_with(engine, records)
    read_watermarks.assert_called_once_with(
        engine,
        tenors=["5Y"],
        end_date="2026-08-25",
    )
    prune.assert_called_once_with(
        engine,
        watermarks,
        end_date="2026-08-25",
    )
    if injected:
        create.assert_not_called()
        engine.dispose.assert_not_called()
    else:
        create.assert_called_once_with()
        engine.dispose.assert_called_once_with()


@pytest.mark.parametrize(
    ("module", "frequency", "calendar_reader", "builder", "writer"),
    (
        (
            weekly_actuals_updater,
            "weekly",
            "read_week_calendar_rows",
            "build_weekly_actual_records_from_rows",
            "upsert_weekly_actuals",
        ),
        (
            monthly_actuals_updater,
            "monthly",
            "read_month_calendar",
            "build_monthly_actual_records_from_rows",
            "upsert_monthly_actuals",
        ),
    ),
)
@pytest.mark.parametrize("injected", [False, True])
def test_period_updater_passes_scope_calendar_and_records(
    module,
    frequency: str,
    calendar_reader: str,
    builder: str,
    writer: str,
    injected: bool,
) -> None:
    engine = MagicMock()
    rows = [{"tenor": "5Y"}]
    calendar_rows = [{"rdate": "2026-08-25"}]
    records = [object()]

    with (
        patch.object(
            module,
            "create_engine_from_env",
            return_value=engine,
        ) as create,
        patch.object(
            module,
            "resolve_actual_tenors",
            return_value=["5Y"],
        ) as resolve,
        patch.object(module, "read_yield_rows", return_value=rows) as read_rows,
        patch.object(
            module,
            calendar_reader,
            return_value=calendar_rows,
        ) as read_calendar,
        patch.object(module, builder, return_value=records) as build,
        patch.object(module, writer, return_value=1) as write,
    ):
        written = module.__dict__[f"update_{frequency}_actuals"](
            start_date="2026-08-01",
            end_date="2026-08-25",
            tenors=["5Y"],
            engine=engine if injected else None,
        )

    assert written == 1
    resolve.assert_called_once_with(
        engine,
        frequency=frequency,
        tenors=["5Y"],
    )
    read_rows.assert_called_once_with(
        engine,
        tenors=["5Y"],
        end_date="2026-08-25",
    )
    read_calendar.assert_called_once_with(engine)
    build.assert_called_once_with(
        rows,
        calendar_rows,
        start_date="2026-08-01",
        end_date="2026-08-25",
    )
    write.assert_called_once_with(engine, records)
    if injected:
        create.assert_not_called()
        engine.dispose.assert_not_called()
    else:
        create.assert_called_once_with()
        engine.dispose.assert_called_once_with()


@pytest.mark.parametrize("injected", [False, True])
def test_period_average_updater_passes_nonempty_scope(
    injected: bool,
) -> None:
    engine = MagicMock()
    rows = [{"tenor": "5Y"}]
    calendar_rows = [{"rdate": "2026-08-25"}]
    records = [object()]
    with (
        patch.object(
            period_average_actuals_updater,
            "create_engine_from_env",
            return_value=engine,
        ) as create,
        patch.object(
            period_average_actuals_updater,
            "active_registry_tenors_by_task_type",
            return_value={"monthly_average": ["5Y"]},
        ) as scope,
        patch.object(
            period_average_actuals_updater,
            "read_yield_rows",
            return_value=rows,
        ) as read_rows,
        patch.object(
            period_average_actuals_updater,
            "read_trade_calendar_rows",
            return_value=calendar_rows,
        ) as read_calendar,
        patch.object(
            period_average_actuals_updater,
            "build_shared_records",
            return_value=records,
        ) as build,
        patch.object(
            period_average_actuals_updater,
            "upsert_period_average_actuals",
            return_value=1,
        ) as write,
    ):
        assert period_average_actuals_updater.update_period_average_actuals(
            start_date="2026-01-01",
            end_date="2026-08-25",
            engine=engine if injected else None,
        ) == 1

    scope.assert_called_once()
    read_rows.assert_called_once_with(
        engine,
        tenors=["5Y"],
        end_date="2026-08-25",
    )
    read_calendar.assert_called_once_with(engine)
    build.assert_called_once_with(
        rows,
        calendar_rows,
        task_types=("monthly_average",),
        start_date="2026-01-01",
        end_date="2026-08-25",
    )
    write.assert_called_once_with(engine, records)
    if injected:
        create.assert_not_called()
        engine.dispose.assert_not_called()
    else:
        create.assert_called_once_with()
        engine.dispose.assert_called_once_with()


@pytest.mark.parametrize(
    ("module", "function_name", "failing_dependency"),
    (
        (daily_actuals_updater, "update_actuals", "resolve_actual_tenors"),
        (
            weekly_actuals_updater,
            "update_weekly_actuals",
            "resolve_actual_tenors",
        ),
        (
            monthly_actuals_updater,
            "update_monthly_actuals",
            "resolve_actual_tenors",
        ),
        (
            period_average_actuals_updater,
            "update_period_average_actuals",
            "active_registry_tenors_by_task_type",
        ),
    ),
)
@pytest.mark.parametrize("injected", [False, True])
def test_updater_exception_preserves_engine_ownership(
    module,
    function_name: str,
    failing_dependency: str,
    injected: bool,
) -> None:
    engine = MagicMock()
    with (
        patch.object(
            module,
            "create_engine_from_env",
            return_value=engine,
        ) as create,
        patch.object(
            module,
            failing_dependency,
            side_effect=RuntimeError("injected failure"),
        ),
        pytest.raises(RuntimeError, match="injected failure"),
    ):
        module.__dict__[function_name](engine=engine if injected else None)

    if injected:
        create.assert_not_called()
        engine.dispose.assert_not_called()
    else:
        create.assert_called_once_with()
        engine.dispose.assert_called_once_with()
