from __future__ import annotations

import unittest


class ArtifactPathsTests(unittest.TestCase):
    def test_benchmark_source_evidence_root_uses_source_evidence_batches(self) -> None:
        from shared.artifact_paths import PROJECT_ROOT, benchmark_source_evidence_root

        self.assertEqual(
            benchmark_source_evidence_root("model_muti_0529"),
            PROJECT_ROOT / "source_evidence" / "benchmark_batches" / "model_muti_0529",
        )


if __name__ == "__main__":
    unittest.main()
