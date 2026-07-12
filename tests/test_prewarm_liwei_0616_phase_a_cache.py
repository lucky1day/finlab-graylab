from __future__ import annotations

import unittest

from scripts.prewarm_liwei_0616_phase_a_cache import PREWARM_SCHEMES, prewarm


class PrewarmLiwei0616PhaseACacheTests(unittest.TestCase):
    """liwei_0616 cache 预热入口测试。"""

    def test_prewarm_runs_three_representative_schemes_without_repository(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_run_scheme(scheme_id: str, predict_date: str) -> list[dict]:
            calls.append((scheme_id, predict_date))
            return [
                {
                    "extra": {
                        "phase_a_cache": {
                            "status": "hit",
                            "watermark": "2026-07-09",
                            "missing_dates": [],
                        }
                    }
                }
            ]

        result = prewarm("2026-07-10", run_scheme_fn=fake_run_scheme)

        self.assertEqual(
            PREWARM_SCHEMES,
            (
                "liwei_0616_cons_sda_k3_div_k10",
                "liwei_0616_7y01_cons_say_k3_div_k10",
                "liwei_0616_10y01_cons_say_k3_div_k10",
            ),
        )
        self.assertEqual(calls, [(scheme_id, "2026-07-10") for scheme_id in PREWARM_SCHEMES])
        self.assertEqual([item["status"] for item in result], ["hit", "hit", "hit"])
        self.assertEqual([item["scheme_id"] for item in result], list(PREWARM_SCHEMES))

    def test_prewarm_rejects_missing_prediction_or_cache_audit(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "returned no prediction records"):
            prewarm("2026-07-10", run_scheme_fn=lambda _scheme, _day: [])

        with self.assertRaisesRegex(RuntimeError, "missing phase_a_cache audit"):
            prewarm("2026-07-10", run_scheme_fn=lambda _scheme, _day: [{"extra": {}}])


if __name__ == "__main__":
    unittest.main()
