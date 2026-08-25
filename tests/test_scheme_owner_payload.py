"""dashboard scheme payload 的 owner 字段契约。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.factor_lab_dashboard import _registry_dto  # noqa: E402
from backend.factor_lab_dashboard_semantics import SCHEME_FIELDS  # noqa: E402


def _row() -> dict:
    return {
        "scheme_id": "demo__h1__10Y",
        "base_scheme_id": "demo",
        "name": "demo scheme",
        "description": "d",
        "horizon": 1,
        "task_type": "T+1",
        "frequency": "daily",
        "target_tenor": "10Y",
        "status": "active",
        "deployed_at": "2026-01-01",
    }


class SchemeOwnerPayloadTests(unittest.TestCase):
    def test_owner_is_a_scheme_payload_field(self) -> None:
        self.assertIn("owner", SCHEME_FIELDS)

    def test_registered_scheme_carries_owner(self) -> None:
        dto = _registry_dto(_row(), {"demo__h1__10Y": "LW"})
        self.assertEqual(dto["owner"], "LW")

    def test_unregistered_scheme_gets_empty_owner(self) -> None:
        dto = _registry_dto(_row(), {})
        self.assertEqual(dto["owner"], "")

    def test_owner_lookup_uses_composite_not_base_id(self) -> None:
        """按 base_scheme_id 登记不生效——键必须是 composite scheme_id。"""
        dto = _registry_dto(_row(), {"demo": "LW"})
        self.assertEqual(dto["owner"], "")
