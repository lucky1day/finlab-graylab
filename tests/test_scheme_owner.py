"""方案归属映射的读取契约。

归属是平台对方案的登记信息，不参与任何计算、gate 或 join。文件本身缺失或
格式非法必须 fail-closed——否则「全部未登记」与「配置坏了」不可区分。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.scheme_owner import SchemeOwnerError, load_scheme_owners  # noqa: E402
from shared.scheme_owner_registry import (  # noqa: E402
    owner_registry_scheme_id,
)


def _write(root: Path, payload: object) -> None:
    target = root / "deploy"
    target.mkdir(parents=True, exist_ok=True)
    (target / "scheme_owner_v1.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


class LoadSchemeOwnersTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_reads_composite_scheme_id_mapping(self) -> None:
        _write(
            self.root,
            {
                "schema_version": "scheme-owner-v1",
                "owners": {"demo__h1__10Y": "LW"},
            },
        )
        self.assertEqual(
            load_scheme_owners(self.root), {"demo__h1__10Y": "LW"}
        )

    def test_builds_canonical_composite_scheme_id(self) -> None:
        self.assertEqual(
            owner_registry_scheme_id("demo", 5, "10Y"),
            "demo__h5__10Y",
        )





    def test_missing_file_fails_closed(self) -> None:
        with self.assertRaises(SchemeOwnerError):
            load_scheme_owners(self.root)




    def test_placeholder_owner_fails_closed(self) -> None:
        _write(
            self.root,
            {
                "schema_version": "scheme-owner-v1",
                "owners": {"demo__h1__10Y": "unknown"},
            },
        )
        with self.assertRaises(SchemeOwnerError):
            load_scheme_owners(self.root)

    def test_repository_file_is_loadable(self) -> None:
        """仓库中的实际文件必须始终可读，否则 dashboard 会整体 fail-closed。"""
        self.assertIsInstance(load_scheme_owners(), dict)

