from __future__ import annotations

import unittest


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


if __name__ == "__main__":
    unittest.main()
