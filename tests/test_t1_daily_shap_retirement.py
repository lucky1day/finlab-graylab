from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

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

if __name__ == "__main__":
    unittest.main()
