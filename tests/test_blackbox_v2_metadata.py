from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from shared.blackbox_v2.contracts import load_metadata


_MISSING = object()


class BlackboxV2MetadataTests(unittest.TestCase):
    def test_contract_1_0_accepts_missing_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata = load_metadata(_write_metadata(Path(tmpdir)))

        self.assertEqual(metadata.schema_version, "1.0")
        self.assertIsNone(metadata.description)

    def test_contract_1_0_accepts_and_strips_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata = load_metadata(
                _write_metadata(
                    Path(tmpdir),
                    description="  使用流动性指标判断未来方向。  ",
                )
            )

        self.assertEqual(metadata.description, "使用流动性指标判断未来方向。")

    def test_rejects_invalid_optional_description(self) -> None:
        invalid_values: tuple[Any, ...] = (
            "",
            [],
            "第一行\n第二行",
            "<b>模型</b>",
            "算" * 301,
        )
        for value in invalid_values:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmpdir:
                with self.assertRaisesRegex(ValueError, "description"):
                    load_metadata(
                        _write_metadata(Path(tmpdir), description=value)
                    )

    def test_rejects_other_undeclared_metadata_fields(self) -> None:
        for field, value in (
            ("platform_note", "平台运营说明"),
            ("platform_inputs", ["api-wind-date-v1"]),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmpdir:
                path = _write_metadata(Path(tmpdir))
                raw = json.loads(path.read_text(encoding="utf-8"))
                raw[field] = value
                path.write_text(
                    json.dumps(raw, ensure_ascii=False),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, "metadata fields mismatch"):
                    load_metadata(path)


def _write_metadata(
    root: Path,
    *,
    description: Any = _MISSING,
) -> Path:
    raw: dict[str, Any] = {
        "schema_version": "1.0",
        "scheme_id": "trial_10y",
        "name": "10Y Trial",
        "algorithm_version": "1.0.0",
        "target_tenor": "10Y",
        "task_type": "T+1",
        "horizon": 1,
        "target_rule": "target_date_yield_vs_feature_date_yield",
    }
    if description is not _MISSING:
        raw["description"] = description
    path = root / "trial_10y.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
