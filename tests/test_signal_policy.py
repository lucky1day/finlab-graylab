"""无信号平台政策测试。"""

from __future__ import annotations

import importlib
import importlib.util
import json
import unittest
from dataclasses import FrozenInstanceError
from types import ModuleType


def _load_signal_policy() -> ModuleType:
    spec = importlib.util.find_spec("shared.signal_policy")
    if spec is None:
        raise AssertionError("shared.signal_policy 尚未实现")
    return importlib.import_module("shared.signal_policy")


class SignalPolicyTests(unittest.TestCase):
    """平台生成的无信号结果统一转为带审计标记的平。"""

    def test_no_signal_as_flat_returns_immutable_flat_outcome(self) -> None:
        signal_policy = _load_signal_policy()

        outcome = signal_policy.no_signal_as_flat(
            "rule_vote",
            extra={"feature_week_id": 202624, "model_scope": "weekly_live"},
        )

        self.assertIsInstance(outcome, signal_policy.SignalOutcome)
        self.assertTrue(
            hasattr(outcome, "predicted_direction"),
            "SignalOutcome 必须统一使用 predicted_direction",
        )
        self.assertEqual(outcome.predicted_direction, 0)
        self.assertEqual(outcome.confidence, 0.0)
        self.assertEqual(outcome.extra["signal_state"], "no_signal")
        self.assertEqual(outcome.extra["signal_policy"], "no_signal_to_flat_v1")
        self.assertIs(outcome.extra["signal_policy_applied"], True)
        self.assertEqual(
            outcome.extra["no_signal_reason"],
            "core_output_missing_current_feature",
        )
        self.assertEqual(outcome.extra["source_component"], "rule_vote")
        self.assertEqual(outcome.extra["feature_week_id"], 202624)
        self.assertEqual(outcome.extra["model_scope"], "weekly_live")

        with self.assertRaises(FrozenInstanceError):
            outcome.predicted_direction = 1
        with self.assertRaises(TypeError):
            outcome.extra["signal_state"] = "signal"

    def test_policy_keys_cannot_be_overridden_by_context_extra(self) -> None:
        signal_policy = _load_signal_policy()

        outcome = signal_policy.no_signal_as_flat(
            "d_overlay",
            extra={
                "signal_state": "signal",
                "signal_policy": "caller_policy",
                "signal_policy_applied": False,
                "no_signal_reason": "caller_reason",
                "source_component": "caller_component",
            },
        )

        self.assertEqual(outcome.extra["signal_state"], "no_signal")
        self.assertEqual(outcome.extra["signal_policy"], "no_signal_to_flat_v1")
        self.assertIs(outcome.extra["signal_policy_applied"], True)
        self.assertEqual(
            outcome.extra["no_signal_reason"],
            "core_output_missing_current_feature",
        )
        self.assertEqual(outcome.extra["source_component"], "d_overlay")

    def test_context_lists_are_copied_and_frozen_recursively(self) -> None:
        signal_policy = _load_signal_policy()
        available_signal_weeks = [202622, 202623]

        outcome = signal_policy.no_signal_as_flat(
            "cross_d_overlay",
            extra={"available_signal_weeks": available_signal_weeks},
        )

        frozen_weeks = outcome.extra["available_signal_weeks"]
        self.assertIsInstance(frozen_weeks, tuple)
        available_signal_weeks.append(202624)
        self.assertEqual(frozen_weeks, (202622, 202623))
        with self.assertRaises(AttributeError):
            frozen_weeks.append(202624)
        json.dumps(dict(outcome.extra))


if __name__ == "__main__":
    unittest.main()
