from __future__ import annotations

import base64
import unittest
from unittest.mock import patch


class DataBridgeClientTests(unittest.TestCase):
    def test_allows_explicit_empty_daily_chunk_but_not_generic_404(self) -> None:
        from shared.data_bridge.client import (
            DataBridgeClient,
            DataBridgeClientConfig,
            DataBridgeRequestError,
            HttpPayload,
        )

        client = DataBridgeClient(
            DataBridgeClientConfig(
                base_url="http://example.test/api/",
                username="ddd",
                password="secret",
                retry_delays_sec=(),
            )
        )
        empty = HttpPayload(
            404,
            {"content-type": "application/json"},
            '{"success":false,"error":"查询结果为空，请调整日期范围后重试。"}'.encode(),
        )
        with patch.object(client, "_request_once", return_value=empty):
            self.assertIsNone(client.export_csv("日", allow_empty=True))
            with self.assertRaises(DataBridgeRequestError):
                client.export_csv("日", allow_empty=False)

    def test_switches_to_read_timeout_before_waiting_for_response(self) -> None:
        from shared.data_bridge.client import DataBridgeClient, DataBridgeClientConfig

        client = DataBridgeClient(
            DataBridgeClientConfig(
                base_url="http://example.test/api/",
                username="ddd",
                password="secret",
                connect_timeout_sec=10,
                read_timeout_sec=180,
                retry_delays_sec=(),
            )
        )
        connection = _FakeConnection()
        with patch("shared.data_bridge.client.http.client.HTTPConnection", return_value=connection):
            client.export_csv("日")

        self.assertEqual(connection.sock.timeouts, [180])
        self.assertTrue(connection.connected_before_request)

    def test_retries_transient_server_errors(self) -> None:
        from shared.data_bridge.client import DataBridgeClient, DataBridgeClientConfig, HttpPayload

        client = DataBridgeClient(
            DataBridgeClientConfig(
                base_url="http://example.test/api/",
                username="ddd",
                password="secret",
                retry_delays_sec=(1, 3, 10),
            )
        )
        responses = [
            HttpPayload(503, {"content-type": "text/plain"}, b"busy"),
            HttpPayload(502, {"content-type": "text/plain"}, b"retry"),
            HttpPayload(200, {"content-type": "text/csv"}, b"date,value\n2026-07-18,1\n"),
        ]
        with (
            patch.object(client, "_request_once", side_effect=responses) as request,
            patch("shared.data_bridge.client.time.sleep") as sleep,
        ):
            payload = client.export_csv("日")

        self.assertEqual(payload.splitlines()[0], b"date,value")
        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 3])

    def test_builds_basic_auth_and_retries_without_exposing_password(self) -> None:
        from shared.data_bridge.client import (
            DataBridgeClient,
            DataBridgeClientConfig,
            HttpPayload,
        )

        config = DataBridgeClientConfig(
            base_url="http://example.test/api/",
            username="ddd",
            password="top-secret",
            connect_timeout_sec=10,
            read_timeout_sec=180,
            retry_delays_sec=(1, 3, 10),
        )
        client = DataBridgeClient(config)
        expected_auth = "Basic " + base64.b64encode(b"ddd:top-secret").decode("ascii")
        attempts: list[dict[str, str]] = []

        def request_once(path: str, params: dict[str, str]) -> HttpPayload:
            attempts.append(dict(client.request_headers))
            if len(attempts) < 3:
                raise OSError("temporary failure mentioning top-secret")
            return HttpPayload(200, {"content-type": "text/csv"}, b"date,value\n2026/07/18 00:00,1\n")

        with patch.object(client, "_request_once", side_effect=request_once):
            with patch("shared.data_bridge.client.time.sleep") as sleep:
                payload = client.export_csv("日", start_date="2026-07-18", end_date="2026-07-18")

        self.assertEqual(payload.splitlines()[0], b"date,value")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(attempts[0]["Authorization"], expected_auth)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 3])
        self.assertNotIn("top-secret", client.redact("failed top-secret Basic abc"))

    def test_rejects_missing_credentials_as_configuration_error(self) -> None:
        from shared.data_bridge.client import DataBridgeConfigurationError, DataBridgeClientConfig

        with patch.dict(
            "os.environ",
            {
                "DATABRIDGE_API_BASE_URL": "http://example.test/api/",
                "DATABRIDGE_API_USERNAME": "ddd",
                "DATABRIDGE_API_PASSWORD": "",
            },
            clear=False,
        ):
            with self.assertRaises(DataBridgeConfigurationError):
                DataBridgeClientConfig.from_env()


class _FakeSocket:
    def __init__(self) -> None:
        self.timeouts: list[int] = []

    def settimeout(self, value: int) -> None:
        self.timeouts.append(value)


class _FakeResponse:
    status = 200

    def read(self) -> bytes:
        return b"date,value\n2026-07-18,1\n"

    def getheaders(self):
        return [("Content-Type", "text/csv")]


class _FakeConnection:
    def __init__(self) -> None:
        self.sock = _FakeSocket()
        self.connected_before_request = False

    def connect(self) -> None:
        self.connected_before_request = True

    def request(self, *_args, **_kwargs) -> None:
        if not self.connected_before_request:
            raise AssertionError("request started before connection was established")

    def getresponse(self) -> _FakeResponse:
        if self.sock.timeouts != [180]:
            raise AssertionError("read timeout was not applied before getresponse")
        return _FakeResponse()

    def close(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
