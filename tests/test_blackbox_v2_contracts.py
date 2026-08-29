from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class BlackboxV2MetadataContractTests(unittest.TestCase):
    def test_owner_is_optional_for_historical_parse_but_validated_when_present(
        self,
    ) -> None:
        historical = _load_metadata(_metadata_payload())
        self.assertIsNone(historical.owner)

        payload = _metadata_payload()
        payload["owner"] = "ALGO-A"
        self.assertEqual(_load_metadata(payload).owner, "ALGO-A")

        for invalid_owner in (
            "",
            " owner",
            "owner ",
            "--",
            "Unknown",
            "待定",
            "owner\nteam",
            "<owner>",
            "x" * 65,
        ):
            with self.subTest(owner=invalid_owner):
                payload["owner"] = invalid_owner
                with self.assertRaisesRegex(ValueError, "owner"):
                    _load_metadata(payload)

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
    def test_prediction_json_accepts_integer_directions(self) -> None:
        for direction in (-1, 0, 1):
            with self.subTest(direction=direction):
                result = _load_prediction_direction(direction)

                self.assertEqual(result.predicted_direction, direction)
                self.assertIs(type(result.predicted_direction), int)

    def test_prediction_json_rejects_invalid_directions(self) -> None:
        for direction in ("1", True, -2, 2):
            with self.subTest(direction=direction):
                with self.assertRaisesRegex(ValueError, "predicted_direction"):
                    _load_prediction_direction(direction)

    def test_backtest_csv_accepts_exact_direction_tokens(self) -> None:
        for token, expected in (("-1", -1), ("0", 0), ("1", 1)):
            with self.subTest(token=token):
                result = _load_backtest_direction(token)

                self.assertEqual(result.predicted_direction, expected)
                self.assertIs(type(result.predicted_direction), int)

    def test_backtest_csv_rejects_invalid_directions(self) -> None:
        for token in ("", None, "-2", "2"):
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


def test_request_csv_fields_match_the_dataclass_exactly() -> None:
    """单条和批量 Request 写出器必须共享精确字段契约。"""
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


def test_upstream_delivery_samples_match_the_machine_contract() -> None:
    """上游包内样例必须可由当前机器合同直接解析。"""
    import pandas as pd

    from shared.blackbox_v2.contracts import (
        load_backtest_results,
        load_metadata,
        load_prediction_result,
        load_request,
        load_requests,
    )
    from shared.data_bridge.validation import validate_dataset

    samples = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "blackbox_v2"
        / "data_bridge_v1"
        / "samples"
    )
    metadata = load_metadata(samples / "metadata.sample.json")
    request = load_request(samples / "request.sample.json")
    requests = load_requests(samples / "requests.sample.csv")
    prediction = load_prediction_result(
        samples / "prediction.sample.json",
        request,
    )
    results = load_backtest_results(
        samples / "backtest.sample.csv",
        requests,
    )
    performance = json.loads(
        (samples / "performance.sample.json").read_text(encoding="utf-8")
    )
    data_files = (
        "daily_output.csv",
        "weekly_output.csv",
            "monthly_output.csv",
            "api_wind_date.csv",
            "factor_catalog.csv",
    )
    dataset = validate_dataset(
        {
            filename: pd.read_csv(
                samples / filename.replace(".csv", ".sample.csv"),
                dtype="string",
            )
            for filename in data_files
        },
        schema_path=(
            Path(__file__).resolve().parents[1]
            / "shared"
            / "blackbox_v2"
            / "data_bridge_v1_schema.json"
        ),
        expected_daily_date="2026-01-07",
    )

    assert metadata.owner == "lw"
    assert requests[0] == request
    assert prediction.request_id == request.request_id
    assert [result.request_id for result in results] == [
        item.request_id for item in requests
    ]
    assert performance["scheme_id"] == metadata.scheme_id
    assert performance["batch_self_check"]["fallback_used"] is False
    assert dataset.files["daily_output.csv"].rows == 4
