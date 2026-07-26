from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
import hashlib
import unittest

import pandas as pd


class BlackboxV2PlatformInputRegistryTests(unittest.TestCase):
    def test_registered_api_wind_date_contract_is_stable(self) -> None:
        from shared.blackbox_v2.platform_input_registry import (
            PLATFORM_INPUT_REGISTRY,
        )

        spec = PLATFORM_INPUT_REGISTRY.get("api-wind-date-v1")

        self.assertEqual(spec.artifact_id, "api-wind-date-v1")
        self.assertEqual(spec.provider_version, "api-wind-date-provider-v1")
        self.assertEqual(spec.filename, "api_wind_date.csv")
        self.assertEqual(spec.columns, ("rdate", "week_id"))

    def test_registry_normalizes_registered_ids_in_sorted_order(self) -> None:
        from shared.blackbox_v2.platform_input_registry import (
            PlatformInputRegistry,
            PlatformInputSpec,
        )

        registry = PlatformInputRegistry(
            (
                PlatformInputSpec(
                    artifact_id="z-context-v1",
                    provider_version="z-provider-v1",
                    filename="z.csv",
                    columns=("z",),
                ),
                PlatformInputSpec(
                    artifact_id="a-context-v1",
                    provider_version="a-provider-v1",
                    filename="a.csv",
                    columns=("a",),
                ),
            )
        )

        self.assertEqual(
            registry.normalize_ids(["z-context-v1", "a-context-v1"]),
            ("a-context-v1", "z-context-v1"),
        )

    def test_registry_rejects_empty_duplicate_unknown_or_unversioned_ids(self) -> None:
        from shared.blackbox_v2.platform_input_registry import (
            PLATFORM_INPUT_REGISTRY,
        )

        invalid_values = (
            [],
            [""],
            ["api-wind-date-v1", "api-wind-date-v1"],
            ["unknown-input-v1"],
            ["api-wind-date"],
            "api-wind-date-v1",
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                PLATFORM_INPUT_REGISTRY.normalize_ids(value)

    def test_registry_rejects_duplicate_filenames_and_unsafe_paths(self) -> None:
        from shared.blackbox_v2.platform_input_registry import (
            PlatformInputRegistry,
            PlatformInputSpec,
        )

        first = PlatformInputSpec(
            artifact_id="first-input-v1",
            provider_version="first-provider-v1",
            filename="shared.csv",
            columns=("value",),
        )
        duplicate_filename = PlatformInputSpec(
            artifact_id="second-input-v1",
            provider_version="second-provider-v1",
            filename="shared.csv",
            columns=("other",),
        )
        with self.assertRaisesRegex(ValueError, "filename"):
            PlatformInputRegistry((first, duplicate_filename))

        for filename in ("../unsafe.csv", r"..\unsafe.csv"):
            with self.subTest(filename=filename):
                unsafe = PlatformInputSpec(
                    artifact_id="unsafe-input-v1",
                    provider_version="unsafe-provider-v1",
                    filename=filename,
                    columns=("value",),
                )
                with self.assertRaisesRegex(ValueError, "filename"):
                    PlatformInputRegistry((unsafe,))


class ApiWindDatePlatformInputProviderTests(unittest.TestCase):
    def test_freezes_canonical_utf8_lf_bytes_without_feature_date_truncation(
        self,
    ) -> None:
        from shared.blackbox_v2.platform_inputs import freeze_platform_input

        frame = pd.DataFrame(
            {
                "rdate": [
                    date(2026, 7, 13),
                    datetime(2026, 7, 14, 9, 30),
                    "2026-07-15",
                    "2026-07-16",
                ],
                "week_id": [202628, 202628.0, "202628", "202628.0"],
            }
        )

        artifact = freeze_platform_input(
            "api-wind-date-v1",
            frame,
            weekly_cutoff_key="202628",
            audit_provenance={
                "source_kind": "test",
                "captured_at": "2026-07-26T00:00:00Z",
            },
        )

        self.assertEqual(
            artifact.content_bytes,
            (
                b"rdate,week_id\n"
                b"2026-07-13,202628\n"
                b"2026-07-14,202628\n"
                b"2026-07-15,202628\n"
                b"2026-07-16,202628\n"
            ),
        )
        self.assertFalse(artifact.content_bytes.endswith(b"\r\n"))
        self.assertEqual(artifact.columns, ("rdate", "week_id"))
        self.assertEqual(artifact.row_count, 4)
        self.assertEqual(artifact.size_bytes, len(artifact.content_bytes))
        self.assertEqual(
            artifact.identity_manifest,
            {
                "artifact_id": "api-wind-date-v1",
                "provider_version": "api-wind-date-provider-v1",
                "filename": "api_wind_date.csv",
                "sha256": artifact.sha256,
                "size_bytes": len(artifact.content_bytes),
                "row_count": 4,
                "columns": ["rdate", "week_id"],
            },
        )

    def test_rejects_wrong_columns_or_empty_frame(self) -> None:
        from shared.blackbox_v2.platform_inputs import freeze_platform_input

        invalid = (
            pd.DataFrame(columns=["rdate", "week_id"]),
            pd.DataFrame({"rdate": ["2026-07-15"]}),
            pd.DataFrame(
                {
                    "rdate": ["2026-07-15"],
                    "week_id": ["202628"],
                    "extra": ["no"],
                }
            ),
            pd.DataFrame(
                {"week_id": ["202628"], "rdate": ["2026-07-15"]}
            ),
        )
        for frame in invalid:
            with self.subTest(columns=list(frame.columns), rows=len(frame)):
                with self.assertRaises(ValueError):
                    freeze_platform_input(
                        "api-wind-date-v1",
                        frame,
                        weekly_cutoff_key="202628",
                    )

    def test_rejects_invalid_duplicate_or_nonascending_dates(self) -> None:
        from shared.blackbox_v2.platform_inputs import freeze_platform_input

        date_columns = (
            [None],
            [""],
            ["2026/07/15"],
            ["2026-02-30"],
            ["2026-07-15", "2026-07-15"],
            ["2026-07-16", "2026-07-15"],
        )
        for dates in date_columns:
            with self.subTest(dates=dates), self.assertRaises(ValueError):
                freeze_platform_input(
                    "api-wind-date-v1",
                    pd.DataFrame(
                        {
                            "rdate": dates,
                            "week_id": ["202628"] * len(dates),
                        }
                    ),
                    weekly_cutoff_key="202628",
                )

    def test_rejects_empty_or_invalid_week_keys(self) -> None:
        from shared.blackbox_v2.platform_inputs import freeze_platform_input

        invalid_keys = (None, "", "20262", "202628.5", "week-28", True)
        for week_id in invalid_keys:
            with self.subTest(week_id=week_id), self.assertRaises(ValueError):
                freeze_platform_input(
                    "api-wind-date-v1",
                    pd.DataFrame(
                        {"rdate": ["2026-07-15"], "week_id": [week_id]}
                    ),
                    weekly_cutoff_key="202628",
                )

    def test_rejects_when_weekly_cutoff_is_not_covered(self) -> None:
        from shared.blackbox_v2.platform_inputs import freeze_platform_input

        with self.assertRaisesRegex(ValueError, "weekly_cutoff_key"):
            freeze_platform_input(
                "api-wind-date-v1",
                pd.DataFrame(
                    {
                        "rdate": ["2026-07-15"],
                        "week_id": ["202627"],
                    }
                ),
                weekly_cutoff_key="202628",
            )

    def test_same_content_has_same_hash_despite_audit_provenance(self) -> None:
        from shared.blackbox_v2.platform_inputs import freeze_platform_input

        frame = pd.DataFrame(
            {
                "rdate": ["2026-07-14", "2026-07-15"],
                "week_id": [202628, "202628.0"],
            }
        )
        harness = freeze_platform_input(
            "api-wind-date-v1",
            frame,
            weekly_cutoff_key=202628.0,
            audit_provenance={
                "source_kind": "harness_database",
                "captured_at": "2026-07-26T00:00:00Z",
            },
        )
        scheduled = freeze_platform_input(
            "api-wind-date-v1",
            frame,
            weekly_cutoff_key="202628",
            audit_provenance={
                "source_kind": "scheduled_native_generation",
                "generation_id": "native-1",
                "manifest_sha256": "a" * 64,
                "captured_at": "2026-07-26T00:01:00Z",
            },
        )

        self.assertEqual(harness.content_bytes, scheduled.content_bytes)
        self.assertEqual(harness.sha256, scheduled.sha256)
        self.assertEqual(
            harness.identity_manifest,
            scheduled.identity_manifest,
        )
        self.assertNotEqual(
            harness.audit_provenance,
            scheduled.audit_provenance,
        )

    def test_frozen_artifact_rejects_forged_identity_fields(self) -> None:
        from shared.blackbox_v2.platform_inputs import freeze_platform_input

        artifact = freeze_platform_input(
            "api-wind-date-v1",
            pd.DataFrame(
                {
                    "rdate": ["2026-07-15"],
                    "week_id": ["202628"],
                }
            ),
            weekly_cutoff_key="202628",
        )
        invalid_overrides = (
            {"artifact_id": "unknown-input-v1"},
            {"provider_version": "forged-provider-v1"},
            {"filename": "forged.csv"},
            {"columns": ("week_id", "rdate")},
            {"sha256": "0" * 64},
            {"size_bytes": artifact.size_bytes + 1},
            {"row_count": -1},
            {"row_count": True},
            {"row_count": artifact.row_count + 1},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides), self.assertRaises(
                ValueError
            ):
                replace(artifact, **overrides)

    def test_frozen_artifact_rejects_noncanonical_csv_bytes(self) -> None:
        from shared.blackbox_v2.platform_inputs import FrozenPlatformInput

        invalid_contents = (
            b"rdate,week_id\n",
            b"week_id,rdate\n202628,2026-07-15\n",
            b"rdate,week_id\r\n2026-07-15,202628\r\n",
            b"rdate,week_id\n2026-07-15,202628",
            b'rdate,week_id\n"2026-07-15","202628"\n',
            b"rdate,week_id\n2026-02-30,202628\n",
            b"rdate,week_id\n2026-07-15,week-28\n",
            (
                b"rdate,week_id\n"
                b"2026-07-15,202628\n"
                b"2026-07-15,202628\n"
            ),
            (
                b"rdate,week_id\n"
                b"2026-07-16,202628\n"
                b"2026-07-15,202628\n"
            ),
            b"\xff",
        )
        for content in invalid_contents:
            with self.subTest(content=content), self.assertRaises(ValueError):
                FrozenPlatformInput(
                    artifact_id="api-wind-date-v1",
                    provider_version="api-wind-date-provider-v1",
                    filename="api_wind_date.csv",
                    columns=("rdate", "week_id"),
                    content_bytes=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                    row_count=max(content.count(b"\n") - 1, 0),
                    audit_provenance={},
                )

    def test_frozen_artifact_rejects_nested_or_non_scalar_provenance(
        self,
    ) -> None:
        from shared.blackbox_v2.platform_inputs import freeze_platform_input

        invalid_provenance = (
            {"nested": {"generation_id": "native-1"}},
            {"items": ["native-1"]},
            {1: "native-1"},
            {"generation_id": 1},
        )
        for provenance in invalid_provenance:
            with self.subTest(provenance=provenance), self.assertRaises(
                ValueError
            ):
                freeze_platform_input(
                    "api-wind-date-v1",
                    pd.DataFrame(
                        {
                            "rdate": ["2026-07-15"],
                            "week_id": ["202628"],
                        }
                    ),
                    weekly_cutoff_key="202628",
                    audit_provenance=provenance,
                )

    def test_frozen_artifact_copies_scalar_provenance(self) -> None:
        from shared.blackbox_v2.platform_inputs import freeze_platform_input

        provenance = {"source_kind": "test"}
        artifact = freeze_platform_input(
            "api-wind-date-v1",
            pd.DataFrame(
                {
                    "rdate": ["2026-07-15"],
                    "week_id": ["202628"],
                }
            ),
            weekly_cutoff_key="202628",
            audit_provenance=provenance,
        )
        provenance["source_kind"] = "mutated"

        self.assertEqual(artifact.audit_provenance["source_kind"], "test")
        self.assertEqual(
            artifact.audit_manifest["provenance"]["source_kind"],
            "test",
        )

    def test_freeze_rejects_empty_non_mapping_provenance(self) -> None:
        from shared.blackbox_v2.platform_inputs import freeze_platform_input

        with self.assertRaisesRegex(ValueError, "audit_provenance"):
            freeze_platform_input(
                "api-wind-date-v1",
                pd.DataFrame(
                    {
                        "rdate": ["2026-07-15"],
                        "week_id": ["202628"],
                    }
                ),
                weekly_cutoff_key="202628",
                audit_provenance=[],
            )


if __name__ == "__main__":
    unittest.main()
