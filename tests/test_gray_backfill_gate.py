from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class GrayBackfillGateTests(unittest.TestCase):
    def test_same_day_refresh_uses_shared_current_and_allows_prior_feature(
        self,
    ) -> None:
        """单点灰度补齐与批量补齐遵守同一同日刷新边界。"""
        from harness.gates.gray_backfill_gate import _gray_backfill_preflight

        data_bridge_config = SimpleNamespace(
            schema_path=Path("/srv/bond/data_bridge_schema.json"),
            data_root=Path("/srv/bond/data_bridge"),
            runtime_root=Path("/srv/bond/data_bridge_runtime"),
        )
        with (
            patch(
                "harness.gates.gray_backfill_gate."
                "DataBridgeRefreshConfig.from_env",
                return_value=data_bridge_config,
            ),
            patch(
                "harness.gates.gray_backfill_gate."
                "read_blackbox_current_state",
                return_value={
                    "generation_id": "full-20260807-test",
                    "refresh_date": "2026-08-07",
                },
            ) as read_current,
            patch(
                "harness.gates.gray_backfill_gate.load_metadata",
                return_value=SimpleNamespace(
                    frequency="daily",
                    horizon=1,
                    target_tenor="10Y",
                ),
            ),
            patch("harness.gates.gray_backfill_gate.get_calendar"),
            patch(
                "harness.gates.gray_backfill_gate.build_daily_live_context",
                return_value=SimpleNamespace(
                    feature_date="2026-08-06",
                    target_date="2026-08-08",
                ),
            ),
            patch(
                "harness.gates.gray_backfill_gate."
                "_read_gray_backfill_database_state",
                return_value=("2026-08-07", 0),
            ),
        ):
            _evidence, errors = _gray_backfill_preflight(
                SimpleNamespace(predict_date="2026-08-07"),
                SimpleNamespace(
                    scheme_id="daily_trial",
                    delivery_metadata=Path("delivery.json"),
                ),
                object(),
            )

        self.assertEqual(errors, [])
        read_current.assert_called_once_with(
            schema_path=data_bridge_config.schema_path,
            data_root=data_bridge_config.data_root,
            refresh_runtime_root=data_bridge_config.runtime_root,
        )
