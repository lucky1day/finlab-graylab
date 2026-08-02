from __future__ import annotations

import unittest

from harness.probes.api_probe import factor_lab_url


class FactorLabUrlTest(unittest.TestCase):
    def test_omits_query_when_filters_are_absent(self) -> None:
        self.assertEqual(
            factor_lab_url("http://127.0.0.1:8000/"),
            "http://127.0.0.1:8000/api/backtests/factor-lab",
        )

    def test_preserves_data_source_filter(self) -> None:
        self.assertEqual(
            factor_lab_url("http://127.0.0.1:8000/", data_source="blackbox v2"),
            (
                "http://127.0.0.1:8000/api/backtests/factor-lab"
                "?data_source=blackbox+v2"
            ),
        )

    def test_supports_benchmark_id_filter(self) -> None:
        self.assertEqual(
            factor_lab_url(
                "http://127.0.0.1:8000",
                benchmark_id="demo benchmark",
            ),
            (
                "http://127.0.0.1:8000/api/backtests/factor-lab"
                "?benchmark_id=demo+benchmark"
            ),
        )

    def test_supports_benchmark_id_and_data_source_filters(self) -> None:
        self.assertEqual(
            factor_lab_url(
                "http://127.0.0.1:8000",
                data_source="source original",
                benchmark_id="demo benchmark",
            ),
            (
                "http://127.0.0.1:8000/api/backtests/factor-lab"
                "?data_source=source+original&benchmark_id=demo+benchmark"
            ),
        )


if __name__ == "__main__":
    unittest.main()
