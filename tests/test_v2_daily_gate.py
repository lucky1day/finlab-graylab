"""Blackbox V2 日级 scheduler Gate 测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scheduler.v2_daily_gate import (
    SCHEMA_VERSION,
    V2DailyGateBlocked,
    gate_record_path,
    load_gate_record,
    require_v2_daily_ready,
    write_gate_record,
)
from shared.data_bridge.refresh import DataBridgeRefreshConfig


class V2DailyGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.config = DataBridgeRefreshConfig(
            data_root=root / "data",
            runtime_root=root / "runtime",
            schema_path=root / "schema.json",
            refresh_start="06:00",
            refresh_deadline="07:00",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_gate(
        self,
        *,
        status: str = "ready",
        generation_id: str = "full-20260722-a",
        business_digest: str = "digest-a",
        restart: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return write_gate_record(
            self.config,
            run_date="2026-07-22",
            status=status,
            checked_at="2026-07-22T07:00:00+08:00",
            generation_id=generation_id,
            refresh_date="2026-07-22",
            expected_daily_date="2026-07-21",
            business_digest=business_digest,
            checks=[{"name": "current_dataset", "status": "passed"}],
            restart=restart,
        )

    def test_ready_gate_round_trips_atomically(self) -> None:
        record = self._write_gate()

        self.assertEqual(load_gate_record(self.config, "2026-07-22"), record)
        self.assertEqual(record["schema_version"], SCHEMA_VERSION)
        self.assertFalse(
            any(
                path.name.startswith(".v2-gate-")
                for path in gate_record_path(self.config, "2026-07-22").parent.iterdir()
            )
        )

    def test_missing_gate_blocks_v2(self) -> None:
        with self.assertRaisesRegex(V2DailyGateBlocked, "missing"):
            require_v2_daily_ready(
                self.config,
                "2026-07-22",
                "2026-07-21",
            )

    def test_blocked_gate_blocks_before_current_dataset_read(self) -> None:
        self._write_gate(status="blocked")

        with (
            patch("scheduler.v2_daily_gate.check_current_dataset") as check,
            self.assertRaisesRegex(V2DailyGateBlocked, "not ready"),
        ):
            require_v2_daily_ready(
                self.config,
                "2026-07-22",
                "2026-07-21",
            )

        check.assert_not_called()

    def test_generation_mismatch_blocks_v2(self) -> None:
        self._write_gate(generation_id="full-20260722-old")
        current = SimpleNamespace(
            state={
                "generation_id": "full-20260722-new",
                "refresh_date": "2026-07-22",
                "business_digest": "digest-a",
            }
        )

        with (
            patch("scheduler.v2_daily_gate.check_current_dataset", return_value=current),
            self.assertRaisesRegex(V2DailyGateBlocked, "generation_id mismatch"),
        ):
            require_v2_daily_ready(
                self.config,
                "2026-07-22",
                "2026-07-21",
            )

    def test_matching_gate_returns_generation_bound_record(self) -> None:
        expected = self._write_gate()
        current = SimpleNamespace(
            state={
                "generation_id": "full-20260722-a",
                "refresh_date": "2026-07-22",
                "business_digest": "digest-a",
            }
        )

        with patch(
            "scheduler.v2_daily_gate.check_current_dataset",
            return_value=current,
        ) as check:
            actual = require_v2_daily_ready(
                self.config,
                "2026-07-22",
                "2026-07-21",
            )

        self.assertEqual(actual, expected)
        check.assert_called_once_with(
            self.config,
            required_refresh_date="2026-07-22",
            expected_daily_date="2026-07-21",
            strict_read_only=True,
            require_source_provenance=True,
        )

    def test_matching_gate_accepts_unverified_restart_metadata(self) -> None:
        expected = self._write_gate(
            restart={"requested": False, "verified": False},
        )
        current = SimpleNamespace(
            state={
                "generation_id": "full-20260722-a",
                "refresh_date": "2026-07-22",
                "business_digest": "digest-a",
            }
        )

        with patch(
            "scheduler.v2_daily_gate.check_current_dataset",
            return_value=current,
        ):
            actual = require_v2_daily_ready(
                self.config,
                "2026-07-22",
                "2026-07-21",
            )

        self.assertEqual(actual, expected)
        self.assertFalse(actual["restart"]["verified"])

    def test_invalid_json_gate_is_blocked(self) -> None:
        path = gate_record_path(self.config, "2026-07-22")
        path.parent.mkdir(parents=True)
        path.write_text("not-json", encoding="utf-8")

        with self.assertRaisesRegex(V2DailyGateBlocked, "invalid"):
            load_gate_record(self.config, "2026-07-22")

    def test_gate_file_contains_no_runtime_object_repr(self) -> None:
        self._write_gate()
        payload = json.loads(
            gate_record_path(self.config, "2026-07-22").read_text(encoding="utf-8")
        )

        self.assertEqual(payload["restart"], {})
        self.assertNotIn("config", payload)


if __name__ == "__main__":
    unittest.main()
