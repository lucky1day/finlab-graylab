"""实盘 canonical 预测选择的周频判定回归测试。

周频方案在同一 ``target_date`` 上可能存在多行预测（补发、重跑、灰度与正式
实盘并存）。canonical 规则要求先取更新的 ``feature_date``，同 feature_date
再取更早的 ``predict_date``；点位方案才按行 id 取最新一行。

判定该走哪条规则的唯一依据是 active Registry 的 ``task_type``。预测行自身的
``horizon`` 与 ``extra`` 都不参与该判定。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.factor_lab_dashboard_semantics import (  # noqa: E402
    DashboardDataError,
    apply_live_prediction_corrections,
    choose_live_prediction_rows,
    registry_task_type_index,
)
from backend.factor_lab_dashboard import (  # noqa: E402
    _read_live_prediction_corrections,
)

DISPLAY_UNTIL = "2026-08-11"
TARGET_DATE = "2026-07-31"
SCHEME_ID = "weekly_10y_lgbm_point_v1"
TENOR = "10Y"


def _row(
    *,
    row_id: int,
    horizon: int,
    predict_date: str,
    feature_date: str,
    direction: int,
    extra: dict | None = None,
) -> dict:
    payload = {"feature_date": feature_date}
    payload.update(extra or {})
    return {
        "id": row_id,
        "scheme_id": SCHEME_ID,
        "target_tenor": TENOR,
        "horizon": horizon,
        "predict_date": predict_date,
        "feature_date": feature_date,
        "target_date": TARGET_DATE,
        "predicted_direction": direction,
        "extra": payload,
    }


def _rows(*, horizon: int, extra: dict | None = None) -> list[dict]:
    """同一 target_date 的两行：id 较小的一行 feature_date 更新，是 canonical。"""
    return [
        _row(
            row_id=10,
            horizon=horizon,
            predict_date="2026-07-25",
            feature_date="2026-07-24",
            direction=1,
            extra=extra,
        ),
        _row(
            row_id=20,
            horizon=horizon,
            predict_date="2026-07-26",
            feature_date="2026-07-23",
            direction=-1,
            extra=extra,
        ),
    ]


def _index(task_type: str, *, horizon: int) -> dict:
    return {(SCHEME_ID, TENOR, horizon): task_type}


class WeeklyCanonicalSelectionTests(unittest.TestCase):
    def _selected(self, rows: list[dict], task_type_by_scheme: dict) -> dict:
        selected = choose_live_prediction_rows(
            rows,
            display_until=DISPLAY_UNTIL,
            task_type_by_scheme=task_type_by_scheme,
        )
        self.assertEqual(len(selected), 1)
        return dict(selected[0])

    def test_registry_weekly_point_selects_newest_feature_date(self) -> None:
        """Registry 判为 weekly_point：取更新的 feature_date。"""
        selected = self._selected(
            _rows(horizon=1),
            _index("weekly_point", horizon=1),
        )
        self.assertEqual(selected["id"], 10)
        self.assertEqual(selected["feature_date"], "2026-07-24")

    def test_registry_weekly_average_selects_newest_feature_date(self) -> None:
        """weekly_average 与 weekly_point 适用同一 canonical 规则。"""
        selected = self._selected(
            _rows(horizon=1),
            _index("weekly_average", horizon=1),
        )
        self.assertEqual(selected["id"], 10)

    def test_legacy_weekly_horizon_still_selects_by_registry(self) -> None:
        """存量 horizon=6 的周频方案同样由 Registry 判定。"""
        selected = self._selected(
            _rows(horizon=6),
            _index("weekly_point", horizon=6),
        )
        self.assertEqual(selected["id"], 10)

    def test_registry_point_task_type_keeps_latest_row_rule(self) -> None:
        """Registry 判为点位：按最新行 id 选择。"""
        selected = self._selected(
            _rows(horizon=1),
            _index("T+1", horizon=1),
        )
        self.assertEqual(selected["id"], 20)

    def test_horizon_six_does_not_imply_weekly(self) -> None:
        """horizon=6 不再隐式代表周频；Registry 判为点位即按点位处理。"""
        selected = self._selected(
            _rows(horizon=6),
            _index("T+5", horizon=6),
        )
        self.assertEqual(selected["id"], 20)

    def test_row_extra_hints_do_not_decide(self) -> None:
        """预测行 extra 里的 task_type / frequency 不参与判定。"""
        selected = self._selected(
            _rows(horizon=1, extra={"task_type": "weekly_point", "frequency": "weekly"}),
            _index("T+1", horizon=1),
        )
        self.assertEqual(selected["id"], 20)

    def test_rows_outside_active_registry_use_point_rule(self) -> None:
        """不在 active Registry 中的行不属于当前业务范围，按点位规则去重。"""
        selected = self._selected(_rows(horizon=1), {})
        self.assertEqual(selected["id"], 20)

    def test_selection_is_order_independent(self) -> None:
        """判据来自 Registry 且分组内恒定，结果不依赖输入顺序。"""
        index = _index("weekly_point", horizon=1)
        rows = _rows(horizon=1)
        forward = self._selected(rows, index)
        backward = self._selected(list(reversed(rows)), index)
        self.assertEqual(forward["id"], backward["id"])
        self.assertEqual(forward["id"], 10)


class RegistryTaskTypeIndexTests(unittest.TestCase):
    @staticmethod
    def _registry_row(**overrides) -> dict:
        row = {
            "base_scheme_id": SCHEME_ID,
            "target_tenor": TENOR,
            "horizon": 1,
            "task_type": "weekly_point",
            "status": "active",
        }
        row.update(overrides)
        return row

    def test_index_key_matches_prediction_row_shape(self) -> None:
        index = registry_task_type_index([self._registry_row()])
        self.assertEqual(index, {(SCHEME_ID, TENOR, 1): "weekly_point"})

    def test_non_active_registry_rows_are_excluded(self) -> None:
        index = registry_task_type_index([self._registry_row(status="paused")])
        self.assertEqual(index, {})

    def test_invalid_horizon_fails_closed(self) -> None:
        with self.assertRaises(DashboardDataError):
            registry_task_type_index([self._registry_row(horizon="weekly")])

    def test_missing_task_type_fails_closed(self) -> None:
        with self.assertRaises(DashboardDataError):
            registry_task_type_index([self._registry_row(task_type="")])


class PredictionCorrectionTests(unittest.TestCase):
    def _prediction(self) -> dict:
        return {
            "id": 41,
            "scheme_id": SCHEME_ID,
            "target_tenor": TENOR,
            "horizon": 1,
            "predict_date": "2026-08-22",
            "feature_date": "2026-08-21",
            "target_date": "2026-08-28",
            "scheme_version": "v1",
            "prediction_phase": "scheduled_live",
            "predicted_direction": 1,
            "extra": {},
        }

    def _correction(self, **overrides) -> dict:
        row = {
            "prediction_id": 41,
            "scheme_id": SCHEME_ID,
            "target_tenor": TENOR,
            "horizon": 1,
            "predict_date": "2026-08-22",
            "feature_date": "2026-08-21",
            "target_date": "2026-08-28",
            "scheme_version": "v1",
            "prediction_phase": "scheduled_live",
            "original_direction": 1,
            "corrected_direction": -1,
            "operation_id": "live-correction-20260824",
        }
        row.update(overrides)
        return row

    def test_applies_to_copy_and_preserves_original_prediction(self) -> None:
        source = self._prediction()
        rows, diagnostics = apply_live_prediction_corrections(
            [source],
            [self._correction()],
        )
        self.assertEqual(source["predicted_direction"], 1)
        self.assertEqual(rows[0]["predicted_direction"], -1)
        self.assertEqual(
            diagnostics,
            {
                "applied_count": 1,
                "operation_ids": ["live-correction-20260824"],
            },
        )

    def test_stale_original_direction_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            DashboardDataError,
            "source identity mismatch",
        ):
            apply_live_prediction_corrections(
                [self._prediction()],
                [self._correction(original_direction=0)],
            )

    def test_version_or_phase_drift_fails_closed(self) -> None:
        for override in (
            {"scheme_version": "v2"},
            {"prediction_phase": "gray_live"},
        ):
            with self.subTest(override=override):
                with self.assertRaisesRegex(
                    DashboardDataError,
                    "source identity mismatch",
                ):
                    apply_live_prediction_corrections(
                        [self._prediction()],
                        [self._correction(**override)],
                    )

    def test_unread_prediction_reference_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            DashboardDataError,
            "unread prediction",
        ):
            apply_live_prediction_corrections(
                [self._prediction()],
                [self._correction(prediction_id=99)],
            )

    def test_dashboard_query_reads_only_requested_corrections(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE t_scheme_prediction_corrections (
                        prediction_id INT, scheme_id TEXT, target_tenor TEXT,
                        horizon INT, predict_date TEXT, feature_date TEXT,
                        target_date TEXT, scheme_version TEXT,
                        prediction_phase TEXT, original_direction INT,
                        corrected_direction INT, operation_id TEXT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO t_scheme_prediction_corrections VALUES
                        (41, :scheme_id, '10Y', 1, '2026-08-22',
                         '2026-08-21', '2026-08-28', 'v1', 'scheduled_live',
                         1, -1, 'op-41'),
                        (99, :scheme_id, '10Y', 1, '2026-08-22',
                         '2026-08-21', '2026-08-28', 'v1', 'scheduled_live',
                         1, 0, 'op-99')
                    """
                ),
                {"scheme_id": SCHEME_ID},
            )
        with engine.connect() as connection:
            corrections = _read_live_prediction_corrections(
                connection,
                [self._prediction()],
            )

        rows, diagnostics = apply_live_prediction_corrections(
            [self._prediction()],
            corrections,
        )
        self.assertEqual([row["prediction_id"] for row in corrections], [41])
        self.assertEqual(rows[0]["predicted_direction"], -1)
        self.assertEqual(diagnostics["operation_ids"], ["op-41"])


if __name__ == "__main__":
    unittest.main()
