from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch


class Daily0529ReproductionParadigmTests(unittest.TestCase):
    """daily 0529 runner 默认只暴露 DB-first 回测输出。"""

    def test_default_reproduction_filters_source_evidence_outputs(self) -> None:
        from backtests import daily_0529_reproduction as runner

        outputs = {
            "t5": [
                _output("t5_daily", "baseline_original_csv"),
                _output("t5_daily", "framework_original_csv"),
                _output("t5_daily", "framework_db_aligned"),
            ],
            "t1": [
                _output("t1_daily", "baseline_original_csv"),
                _output("t1_daily", "framework_db_aligned"),
            ],
        }

        with _patched_daily_runner(runner, outputs):
            result = runner.run_daily_0529_reproduction(persist=False)

        self.assertEqual(
            [(item["scheme_id"], item["data_source"]) for item in result["runs"]],
            [("t5_daily", "framework_db_aligned"), ("t1_daily", "framework_db_aligned")],
        )
        self.assertEqual(result["run_ids"], [])

    def test_source_evidence_mode_keeps_csv_outputs(self) -> None:
        from backtests import daily_0529_reproduction as runner

        outputs = {
            "t5": [
                _output("t5_daily", "baseline_original_csv"),
                _output("t5_daily", "framework_db_aligned"),
            ],
            "t1": [
                _output("t1_daily", "framework_original_csv"),
                _output("t1_daily", "framework_db_aligned"),
            ],
        }

        with _patched_daily_runner(runner, outputs):
            result = runner.run_daily_0529_reproduction(
                persist=False,
                include_source_evidence=True,
            )

        self.assertEqual(
            [(item["scheme_id"], item["data_source"]) for item in result["runs"]],
            [
                ("t5_daily", "baseline_original_csv"),
                ("t5_daily", "framework_db_aligned"),
                ("t1_daily", "framework_original_csv"),
                ("t1_daily", "framework_db_aligned"),
            ],
        )


def _output(scheme_id: str, data_source: str) -> SimpleNamespace:
    return SimpleNamespace(scheme_id=scheme_id, data_source=data_source, rows=[{}], summary={})


def _patched_daily_runner(runner, outputs: dict[str, list[SimpleNamespace]]) -> ExitStack:
    engine = SimpleNamespace(dispose=lambda: None)
    stack = ExitStack()
    stack.enter_context(patch.object(runner, "create_sqlalchemy_engine", return_value=engine))
    stack.enter_context(patch.object(runner, "run_data_alignment_check", return_value={"status": "passed"}))
    stack.enter_context(patch.object(runner, "run_t5_reproduction", return_value=outputs["t5"]))
    stack.enter_context(patch.object(runner, "run_t1_reproduction", return_value=outputs["t1"]))
    stack.enter_context(patch.object(runner, "persist_run_output"))
    return stack


if __name__ == "__main__":
    unittest.main()
