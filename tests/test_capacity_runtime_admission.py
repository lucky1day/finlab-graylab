from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


class CapacityRuntimeAdmissionTests(unittest.TestCase):
    def test_signed_candidate_must_equal_current_runtime_candidate(
        self,
    ) -> None:
        from scheduler.capacity_runtime_admission import (
            require_current_capacity_admission,
        )

        engine = object()
        signed_candidate = {"schema_version": "candidate", "value": 1}
        current_candidate = {"schema_version": "candidate", "value": 1}
        admission = {
            "status": "ADMITTED",
            "candidate": signed_candidate,
            "candidate_fingerprint": "a" * 64,
        }
        verified = SimpleNamespace(
            payload=current_candidate,
            fingerprint="a" * 64,
        )
        discovered = (SimpleNamespace(scheme_id="alpha"),)

        with (
            patch(
                "scheduler.capacity_runtime_admission."
                "require_daily_capacity_admission",
                return_value=admission,
            ) as require_signed,
            patch(
                "scheduler.capacity_runtime_admission."
                "build_current_capacity_candidate",
                return_value=SimpleNamespace(
                    payload=current_candidate,
                    fingerprint="a" * 64,
                ),
            ) as build_current,
            patch(
                "scheduler.capacity_runtime_admission."
                "verify_current_candidate",
                return_value=verified,
            ) as verify,
        ):
            result = require_current_capacity_admission(
                engine,
                project_root="/project",
                policy_path="/project/policy.json",
                discovered=discovered,
                algo_env="forecast_env",
            )

        self.assertEqual(result["candidate_fingerprint"], "a" * 64)
        self.assertEqual(result["current_candidate"], current_candidate)
        require_signed.assert_called_once_with(
            policy_path="/project/policy.json",
        )
        build_current.assert_called_once_with(
            engine,
            project_root="/project",
            policy_path="/project/policy.json",
            discovered=discovered,
            native_env="forecast_env",
        )
        verify.assert_called_once_with(
            signed_candidate,
            current_candidate,
        )

    def test_nondefault_execution_environment_is_rejected_before_probe(
        self,
    ) -> None:
        from scheduler.capacity_admission import CapacityAdmissionError
        from scheduler.capacity_runtime_admission import (
            require_current_capacity_admission,
        )

        admission = {
            "status": "ADMITTED",
            "candidate": {"schema_version": "candidate"},
            "candidate_fingerprint": "a" * 64,
        }
        build_current = Mock()
        with (
            patch(
                "scheduler.capacity_runtime_admission."
                "require_daily_capacity_admission",
                return_value=admission,
            ),
            patch(
                "scheduler.capacity_runtime_admission."
                "build_current_capacity_candidate",
                build_current,
            ),
            self.assertRaisesRegex(
                CapacityAdmissionError,
                "algorithm environment",
            ),
        ):
            require_current_capacity_admission(
                object(),
                project_root="/project",
                policy_path="/project/policy.json",
                algo_env="unattested-env",
            )

        build_current.assert_not_called()

    def test_signed_admission_is_checked_before_expensive_current_probe(
        self,
    ) -> None:
        from scheduler.capacity_admission import CapacityAdmissionError
        from scheduler.capacity_runtime_admission import (
            require_current_capacity_admission,
        )

        build_current = Mock()
        with (
            patch(
                "scheduler.capacity_runtime_admission."
                "require_daily_capacity_admission",
                side_effect=CapacityAdmissionError("blocked"),
            ),
            patch(
                "scheduler.capacity_runtime_admission."
                "build_current_capacity_candidate",
                build_current,
            ),
            self.assertRaisesRegex(CapacityAdmissionError, "blocked"),
        ):
            require_current_capacity_admission(
                object(),
                project_root="/project",
                policy_path="/project/policy.json",
            )

        build_current.assert_not_called()

    def test_current_probe_or_fingerprint_failure_is_fail_closed(
        self,
    ) -> None:
        from scheduler.capacity_admission import CapacityAdmissionError
        from scheduler.capacity_candidate_runtime import (
            CapacityCandidateRuntimeError,
        )
        from scheduler.capacity_runtime_admission import (
            require_current_capacity_admission,
        )

        admission = {
            "status": "ADMITTED",
            "candidate": {"schema_version": "candidate"},
            "candidate_fingerprint": "b" * 64,
        }
        with (
            patch(
                "scheduler.capacity_runtime_admission."
                "require_daily_capacity_admission",
                return_value=admission,
            ),
            patch(
                "scheduler.capacity_runtime_admission."
                "build_current_capacity_candidate",
                side_effect=CapacityCandidateRuntimeError("drift"),
            ),
            self.assertRaisesRegex(
                CapacityAdmissionError,
                "current capacity candidate",
            ),
        ):
            require_current_capacity_admission(
                object(),
                project_root="/project",
                policy_path="/project/policy.json",
            )


if __name__ == "__main__":
    unittest.main()
