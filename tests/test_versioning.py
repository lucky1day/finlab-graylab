from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


def _write_scheme(root: Path, *, scheme_id: str = "demo_daily") -> Path:
    scheme_dir = root / scheme_id
    (scheme_dir / "core").mkdir(parents=True)
    (scheme_dir / "predict.py").write_text("def run(predict_date):\n    return []\n", encoding="utf-8")
    (scheme_dir / "core" / "model.py").write_text("def predict(df):\n    return 1\n", encoding="utf-8")
    (scheme_dir / "config.yaml").write_text(
        "\n".join(
            [
                f"scheme_id: {scheme_id}",
                'name: "Demo"',
                'description: "Demo scheme"',
                "horizon: 1",
                'tenors: ["10Y"]',
                "frequency: daily",
                "schedule:",
                '  cron: "25 9 * * 1-5"',
                '  timezone: "Asia/Shanghai"',
                "entry_point: predict.run",
                "status: active",
            ]
        ),
        encoding="utf-8",
    )
    return scheme_dir


class VersioningTests(unittest.TestCase):
    def test_code_hash_is_stable_and_changes_when_scheme_code_changes(self) -> None:
        from shared.versioning import compute_code_hash

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_scheme(Path(tmpdir))
            first = compute_code_hash(scheme_dir)
            second = compute_code_hash(scheme_dir)
            (scheme_dir / "core" / "model.py").write_text("def predict(df):\n    return -1\n", encoding="utf-8")
            changed = compute_code_hash(scheme_dir)

        self.assertEqual(len(first), 64)
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_config_hash_changes_when_config_changes_and_manifest_hash_is_optional(self) -> None:
        from shared.versioning import compute_config_hash, compute_manifest_hash

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_scheme(Path(tmpdir))
            config_path = scheme_dir / "config.yaml"
            first = compute_config_hash(config_path)
            config_path.write_text(config_path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
            changed = compute_config_hash(config_path)
            missing_manifest = compute_manifest_hash(scheme_dir)
            (scheme_dir / "manifest.json").write_text('{"model": "demo"}\n', encoding="utf-8")
            manifest_hash = compute_manifest_hash(scheme_dir)

        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, changed)
        self.assertIsNone(missing_manifest)
        self.assertEqual(len(manifest_hash or ""), 64)

    def test_load_scheme_config_attaches_hashes_and_scheme_version(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_scheme(Path(tmpdir))
            cfg = load_scheme_config(scheme_dir / "config.yaml")

        self.assertEqual(len(cfg.code_hash), 64)
        self.assertEqual(len(cfg.config_hash), 64)
        self.assertIsNone(cfg.manifest_hash)
        self.assertEqual(cfg.scheme_version, cfg.code_hash[:12])


if __name__ == "__main__":
    unittest.main()
