from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class BlackboxV2RequestContractTests(unittest.TestCase):
    def test_request_can_be_strictly_parsed_from_verified_bytes(self) -> None:
        from shared.blackbox_v2.contracts import load_request_bytes

        request = load_request_bytes(
            (
                b'{"request_id":"r1","predict_date":"2026-07-15",'
                b'"feature_date":"2026-07-15","target_date":"2026-07-16",'
                b'"daily_cutoff_key":"2026-07-15",'
                b'"weekly_cutoff_key":"202627",'
                b'"monthly_cutoff_key":"202606"}\n'
            ),
            source="verified Request",
        )

        self.assertEqual(request.request_id, "r1")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            load_request_bytes(
                (
                    b'{"request_id":"r1","request_id":"r2",'
                    b'"predict_date":"2026-07-15",'
                    b'"feature_date":"2026-07-15",'
                    b'"target_date":"2026-07-16",'
                    b'"daily_cutoff_key":"2026-07-15",'
                    b'"weekly_cutoff_key":"202627",'
                    b'"monthly_cutoff_key":"202606"}'
                ),
                source="verified Request",
            )

    def test_reads_strict_single_request(self) -> None:
        from shared.blackbox_v2.contracts import load_request

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "request.json"
            path.write_text(json.dumps(_request_payload()), encoding="utf-8")

            request = load_request(path)

        self.assertEqual(request.request_id, "request-001")
        self.assertEqual(request.weekly_cutoff_key, "202628")
        self.assertEqual(request.monthly_cutoff_key, "202607")

    def test_rejects_extra_request_field(self) -> None:
        from shared.blackbox_v2.contracts import load_request

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "request.json"
            payload = _request_payload()
            payload["unexpected"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Request fields mismatch"):
                load_request(path)

    def test_rejects_duplicate_request_ids_in_batch(self) -> None:
        from shared.blackbox_v2.contracts import load_requests

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "requests.csv"
            path.write_text(
                "request_id,predict_date,feature_date,target_date,daily_cutoff_key,weekly_cutoff_key,monthly_cutoff_key\n"
                "duplicate,2026-07-16,2026-07-15,2026-07-17,2026-07-15,202628,202607\n"
                "duplicate,2026-07-17,2026-07-16,2026-07-20,2026-07-16,202628,202607\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate request_id"):
                load_requests(path)


class BlackboxV2ResultContractTests(unittest.TestCase):
    def test_single_result_must_echo_request(self) -> None:
        from shared.blackbox_v2.contracts import load_prediction_result, request_from_mapping

        request = request_from_mapping(_request_payload())
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "prediction.json"
            path.write_text(json.dumps(_result_payload()), encoding="utf-8")

            result = load_prediction_result(path, request)

        self.assertEqual(result.predicted_direction, -1)

    def test_prediction_json_accepts_integer_directions(self) -> None:
        for direction in (-1, 0, 1):
            with self.subTest(direction=direction):
                result = _load_prediction_direction(direction)

                self.assertEqual(result.predicted_direction, direction)
                self.assertIs(type(result.predicted_direction), int)

    def test_prediction_json_rejects_string_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "predicted_direction"):
            _load_prediction_direction("1")

    def test_prediction_json_rejects_float_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "predicted_direction"):
            _load_prediction_direction(1.0)

    def test_prediction_json_rejects_boolean_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "predicted_direction"):
            _load_prediction_direction(True)

    def test_prediction_json_rejects_null_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "predicted_direction"):
            _load_prediction_direction(None)

    def test_prediction_json_rejects_out_of_range_integer_direction(self) -> None:
        for direction in (-2, 2):
            with self.subTest(direction=direction):
                with self.assertRaisesRegex(ValueError, "predicted_direction"):
                    _load_prediction_direction(direction)

    def test_backtest_csv_accepts_exact_direction_tokens(self) -> None:
        for token, expected in (("-1", -1), ("0", 0), ("1", 1)):
            with self.subTest(token=token):
                result = _load_backtest_direction(token)

                self.assertEqual(result.predicted_direction, expected)
                self.assertIs(type(result.predicted_direction), int)

    def test_backtest_csv_rejects_decimal_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "predicted_direction"):
            _load_backtest_direction("1.0")

    def test_backtest_csv_rejects_plus_prefixed_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "predicted_direction"):
            _load_backtest_direction("+1")

    def test_backtest_csv_rejects_leading_whitespace_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "predicted_direction"):
            _load_backtest_direction(" 1")

    def test_backtest_csv_rejects_trailing_whitespace_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "predicted_direction"):
            _load_backtest_direction("1 ")

    def test_backtest_csv_rejects_empty_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "predicted_direction"):
            _load_backtest_direction("")

    def test_backtest_csv_rejects_missing_direction_as_null(self) -> None:
        with self.assertRaisesRegex(ValueError, "predicted_direction"):
            _load_backtest_direction(None)

    def test_backtest_csv_rejects_null_text_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "predicted_direction"):
            _load_backtest_direction("null")

    def test_backtest_csv_rejects_out_of_range_direction(self) -> None:
        for token in ("-2", "2"):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ValueError, "predicted_direction"):
                    _load_backtest_direction(token)

    def test_batch_result_preserves_request_order(self) -> None:
        from shared.blackbox_v2.contracts import load_backtest_results, request_from_mapping

        first = request_from_mapping(_request_payload())
        second_payload = _request_payload()
        second_payload.update(
            request_id="request-002",
            predict_date="2026-07-17",
            feature_date="2026-07-16",
            target_date="2026-07-20",
            daily_cutoff_key="2026-07-16",
        )
        second = request_from_mapping(second_payload)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "backtest.csv"
            path.write_text(
                "request_id,predict_date,feature_date,target_date,predicted_direction\n"
                "request-002,2026-07-17,2026-07-16,2026-07-20,1\n"
                "request-001,2026-07-16,2026-07-15,2026-07-17,-1\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "order"):
                load_backtest_results(path, [first, second])


def _request_payload() -> dict:
    return {
        "request_id": "request-001",
        "predict_date": "2026-07-16",
        "feature_date": "2026-07-15",
        "target_date": "2026-07-17",
        "daily_cutoff_key": "2026-07-15",
        "weekly_cutoff_key": "202628",
        "monthly_cutoff_key": "202607",
    }


def _result_payload() -> dict:
    return {
        "request_id": "request-001",
        "predict_date": "2026-07-16",
        "feature_date": "2026-07-15",
        "target_date": "2026-07-17",
        "predicted_direction": -1,
    }


def _load_prediction_direction(direction: object):
    from shared.blackbox_v2.contracts import load_prediction_result, request_from_mapping

    request = request_from_mapping(_request_payload())
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "prediction.json"
        payload = _result_payload()
        payload["predicted_direction"] = direction
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_prediction_result(path, request)


def _load_backtest_direction(direction: str | None):
    from shared.blackbox_v2.contracts import load_backtest_results, request_from_mapping

    request = request_from_mapping(_request_payload())
    direction_cell = "" if direction is None else f",{direction}"
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "backtest.csv"
        path.write_text(
            "request_id,predict_date,feature_date,target_date,predicted_direction\n"
            "request-001,2026-07-16,2026-07-15,2026-07-17"
            f"{direction_cell}\n",
            encoding="utf-8",
        )
        return load_backtest_results(path, [request])[0]


if __name__ == "__main__":
    unittest.main()
