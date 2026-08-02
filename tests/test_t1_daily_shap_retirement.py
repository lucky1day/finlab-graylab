from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from scheduler.discovery import load_scheme_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEME_ROOT = PROJECT_ROOT / "schemes" / "t1_daily"
RUNTIME_PATH = SCHEME_ROOT / "core" / "shap_analysis.py"
ARCHIVE_PATH = (
    PROJECT_ROOT
    / "source_evidence"
    / "benchmark_batches"
    / "model_muti_0529"
    / "retired"
    / "t1_daily"
    / "shap_analysis.py.source"
)
MANIFEST_PATH = (
    PROJECT_ROOT
    / "source_evidence"
    / "benchmark_batches"
    / "model_muti_0529"
    / "manifest.json"
)
ARCHIVE_SHA256 = (
    "34597d21b02cfa852c8cadcbcb93ab5f47a17f52f3a400e8557dcfc79fddcdaf"
)
RETIRED_ARTIFACT = {
    "original_runtime_path": "schemes/t1_daily/core/shap_analysis.py",
    "archive_path": (
        "source_evidence/benchmark_batches/model_muti_0529/retired/"
        "t1_daily/shap_analysis.py.source"
    ),
    "sha256": ARCHIVE_SHA256,
    "retirement_reason": (
        "Runtime-unreachable SHAP explanation implementation retired from the "
        "active Native bundle; retained for source-fidelity audit only."
    ),
}
REMAINING_RUNTIME_SHA256 = {
    "predict.py": "3a34afb34326a5923cef81ba6939b84c77e5e0b3617849708a3b37fbe938946f",
    "core/__init__.py": (
        "46c7bc0dc84f3f1b3330138205b0366e288b135fa6918bb61cbae5ee3ecb25a7"
    ),
    "core/config.py": (
        "d93c2c42c97e5b2db6e08253ada6cc58f52507b6585b90181e71c3989e5a3c7b"
    ),
    "core/feature_engineering.py": (
        "10d0be9e840cf28f8bb623412bcdf92d807193fcf33aeaeced0b17641fc11b0a"
    ),
    "core/lgbm_predictor.py": (
        "955be5036d3a8def22f87cdaeddd352e718654be5498f59e04f6bdf67229508f"
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class T1DailyShapRetirementTests(unittest.TestCase):
    def test_runtime_module_is_absent(self) -> None:
        self.assertFalse(RUNTIME_PATH.exists())

    def test_archive_is_outside_scheme_tree_and_preserves_exact_bytes(self) -> None:
        self.assertTrue(ARCHIVE_PATH.is_file())
        self.assertEqual(ARCHIVE_PATH.suffix, ".source")
        self.assertFalse(ARCHIVE_PATH.is_relative_to(SCHEME_ROOT))
        self.assertEqual(_sha256(ARCHIVE_PATH), ARCHIVE_SHA256)

    def test_manifest_declares_exact_retired_artifact(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        matches = [
            artifact
            for artifact in manifest["retired_artifacts"]
            if artifact.get("original_runtime_path")
            == RETIRED_ARTIFACT["original_runtime_path"]
        ]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0], RETIRED_ARTIFACT)

    def test_remaining_runtime_file_hashes_are_unchanged(self) -> None:
        actual = {
            relative_path: _sha256(SCHEME_ROOT / relative_path)
            for relative_path in REMAINING_RUNTIME_SHA256
        }

        self.assertEqual(actual, REMAINING_RUNTIME_SHA256)

    def test_discovery_hashes_exact_shap_retired_version(self) -> None:
        config = load_scheme_config(SCHEME_ROOT / "config.yaml")

        self.assertEqual(
            config.config_hash,
            "e56ff6c4379ae4d65cfad24d327d250442358093606924f86f0efae0a7c02f29",
        )
        self.assertEqual(
            config.code_hash,
            "f86ef896620f3793df6bfaefe8063776f2203762f3e20ff61c1d32336be340e5",
        )
        self.assertEqual(config.scheme_version, "7898b9e47a9a")


if __name__ == "__main__":
    unittest.main()
