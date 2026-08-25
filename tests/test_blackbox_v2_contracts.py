from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class BlackboxV2MetadataContractTests(unittest.TestCase):
    def test_period_average_metadata_uses_bucket_horizon_one(self) -> None:
        cases = (
            (
                "monthly_average",
                "target_month_average_yield_vs_feature_month_average_yield",
                "monthly",
            ),
            (
                "quarterly_average",
                "target_quarter_average_yield_vs_feature_quarter_average_yield",
                "quarterly",
            ),
            (
                "annual_average",
                "target_year_average_yield_vs_feature_year_average_yield",
                "annual",
            ),
        )
        for task_type, target_rule, frequency in cases:
            with self.subTest(task_type=task_type):
                payload = _metadata_payload()
                payload.update(task_type=task_type, horizon=1, target_rule=target_rule)

                metadata = _load_metadata(payload)

                self.assertEqual(metadata.frequency, frequency)

    def test_period_average_metadata_rejects_fixed_day_horizons(self) -> None:
        for task_type, horizon, target_rule in (
            (
                "monthly_average",
                30,
                "target_month_average_yield_vs_feature_month_average_yield",
            ),
            (
                "quarterly_average",
                90,
                "target_quarter_average_yield_vs_feature_quarter_average_yield",
            ),
            (
                "annual_average",
                365,
                "target_year_average_yield_vs_feature_year_average_yield",
            ),
        ):
            with self.subTest(task_type=task_type):
                payload = _metadata_payload()
                payload.update(
                    task_type=task_type,
                    horizon=horizon,
                    target_rule=target_rule,
                )

                with self.assertRaisesRegex(ValueError, "fixed combination"):
                    _load_metadata(payload)

    def test_owner_is_optional_for_immutable_historical_metadata(self) -> None:
        metadata = _load_metadata(_metadata_payload())

        self.assertIsNone(metadata.owner)

    def test_owner_is_normalized_when_present(self) -> None:
        payload = _metadata_payload()
        payload["owner"] = "  ALGO-A  "

        metadata = _load_metadata(payload)

        self.assertEqual(metadata.owner, "ALGO-A")

    def test_owner_rejects_ambiguous_or_placeholder_values(self) -> None:
        for owner in ("", "   ", "ALGO\nA", "<ALGO-A>", "--", "unknown", "UNKNOWN", "待定"):
            with self.subTest(owner=owner):
                payload = _metadata_payload()
                payload["owner"] = owner

                with self.assertRaisesRegex(ValueError, "owner"):
                    _load_metadata(payload)


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


    def test_prediction_json_rejects_boolean_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "predicted_direction"):
            _load_prediction_direction(True)


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





    def test_backtest_csv_rejects_empty_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "predicted_direction"):
            _load_backtest_direction("")

    def test_backtest_csv_rejects_missing_direction_as_null(self) -> None:
        with self.assertRaisesRegex(ValueError, "predicted_direction"):
            _load_backtest_direction(None)


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


def _metadata_payload() -> dict:
    return {
        "schema_version": "1.0",
        "scheme_id": "trial_10y",
        "name": "10Y Trial",
        "algorithm_version": "1.0.0",
        "target_tenor": "10Y",
        "task_type": "T+1",
        "horizon": 1,
        "target_rule": "target_date_yield_vs_feature_date_yield",
    }


def _load_metadata(payload: dict):
    from shared.blackbox_v2.contracts import load_metadata

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "metadata.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return load_metadata(path)


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


# --------------------------------------------------------------------------
# predict 与 backtest 两条路径的 Request 序列化必须同源
# --------------------------------------------------------------------------


def test_request_csv_fields_match_the_dataclass_exactly() -> None:
    """`write_request` 走 asdict 全字段，`write_requests` 走固定 REQUEST_FIELDS。

    两者一旦不同源，交付在 predict 与 backtest 两条路径上会收到不同的输入，
    从而合法地给出不同答案——那是平台的缺陷，不是算法的。此处用毫秒级断言钉死，
    取代此前由 CompareGate 花 3 次全量拟合顺带覆盖的这一小片风险。
    """
    from dataclasses import fields

    from shared.blackbox_v2.contracts import REQUEST_FIELDS, BlackboxRequest

    assert tuple(f.name for f in fields(BlackboxRequest)) == tuple(REQUEST_FIELDS)


def test_both_request_writers_round_trip_to_the_same_values(tmp_path) -> None:
    """同一条 Request 经两个写出器后，字段值必须逐一相同。"""
    import csv
    import json

    from shared.blackbox_v2.contracts import REQUEST_FIELDS, BlackboxRequest
    from shared.blackbox_v2.requests import write_request, write_requests

    request = BlackboxRequest(
        request_id="r-1",
        predict_date="2026-08-22",
        feature_date="2026-08-21",
        target_date="2026-08-28",
        daily_cutoff_key="2026-08-21",
        weekly_cutoff_key="2026-08-21",
        monthly_cutoff_key="2026-07-31",
    )

    single = json.loads(write_request(request, tmp_path / "request.json").read_text("utf-8"))
    with (write_requests([request], tmp_path / "requests.csv")).open(encoding="utf-8") as handle:
        batched = next(iter(csv.DictReader(handle)))

    assert {field: single[field] for field in REQUEST_FIELDS} == dict(batched)
