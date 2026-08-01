from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _ScalarResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _RowResult:
    def __init__(self, row) -> None:
        self._row = row

    def one_or_none(self):
        return self._row


class _Connection:
    def __init__(self, *, active_registry_count: int, version_row) -> None:
        self.active_registry_count = active_registry_count
        self.version_row = version_row
        self.calls: list[str] = []

    def execute(self, sql, _params):
        sql_text = str(sql)
        self.calls.append(sql_text)
        if "COUNT(*)" in sql_text:
            return _ScalarResult(self.active_registry_count)
        return _RowResult(self.version_row)


class _Begin:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self) -> _Connection:
        return self.connection

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _Engine:
    def __init__(self, *, active_registry_count: int = 1, version_row=("active",)) -> None:
        self.connection = _Connection(
            active_registry_count=active_registry_count,
            version_row=version_row,
        )
        self.begin_count = 0

    def begin(self) -> _Begin:
        self.begin_count += 1
        return _Begin(self.connection)


class NativeExecutorActivationTests(unittest.TestCase):
    def test_empty_version_fails_before_registry_query(self) -> None:
        from scheduler.executor import _verify_scheme_activation

        engine = _Engine()

        ok, reason = _verify_scheme_activation(engine, "native_daily", "")

        self.assertFalse(ok)
        self.assertIn("scheme_version is required", reason)
        self.assertEqual(engine.begin_count, 0)

    def test_exact_version_must_exist(self) -> None:
        from scheduler.executor import _verify_scheme_activation

        ok, reason = _verify_scheme_activation(
            _Engine(version_row=None),
            "native_daily",
            "native-version-1",
        )

        self.assertFalse(ok)
        self.assertIn("not found in t_scheme_versions", reason)

    def test_shadow_version_is_not_executable(self) -> None:
        from scheduler.executor import _verify_scheme_activation

        ok, reason = _verify_scheme_activation(
            _Engine(version_row=("shadow",)),
            "native_daily",
            "native-version-1",
        )

        self.assertFalse(ok)
        self.assertIn("must be active", reason)

    def test_legacy_active_version_without_approval_columns_remains_executable(self) -> None:
        from scheduler.executor import _verify_scheme_activation

        ok, reason = _verify_scheme_activation(
            _Engine(version_row=("active",)),
            "native_daily",
            "native-version-1",
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")


class NativeActivationDocumentationTests(unittest.TestCase):
    def test_native_sop_pins_activation_token_to_validation_version_and_issuer(self) -> None:
        sop = (
            PROJECT_ROOT / "docs" / "sop" / "NATIVE_V1_MAINTENANCE_SOP.md"
        ).read_text(encoding="utf-8")

        self.assertIn("validation_scheme_version", sop)
        self.assertIn("--action activate", sop)
        self.assertIn("--scheme-version", sop)
        self.assertIn("--issued-by", sop)


if __name__ == "__main__":
    unittest.main()
