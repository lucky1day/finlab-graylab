from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BlackboxV2RuntimeProfileTests(unittest.TestCase):
    def test_profile_matches_runner_defaults(self) -> None:
        from scheduler.blackbox_v2_runner import DEFAULT_RUNTIME_PROFILE

        profile = json.loads(
            (PROJECT_ROOT / "deploy" / "blackbox_v2" / "runtime_profile_v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(profile["profile_name"], DEFAULT_RUNTIME_PROFILE.name)
        self.assertEqual(profile["conda_env"], DEFAULT_RUNTIME_PROFILE.conda_env)
        for field in (
            "cpu_threads",
            "memory_limit_bytes",
            "predict_timeout_sec",
            "backtest_timeout_sec",
            "max_batch_requests",
            "max_output_bytes",
            "max_log_bytes",
            "sandbox_enabled",
        ):
            self.assertEqual(profile[field], getattr(DEFAULT_RUNTIME_PROFILE, field))

    def test_environment_manifest_fingerprint_is_self_consistent(self) -> None:
        manifest = json.loads(
            (PROJECT_ROOT / "deploy" / "blackbox_v2" / "environment_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        canonical = json.dumps(
            manifest["explicit_packages"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(
            manifest["environment_fingerprint"],
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
