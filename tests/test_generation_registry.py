from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_databridge_input_generation import (
    SCHEMA_PATH,
    _current_dataset,
    _native_cutoff_context,
)
from tests.test_native_generation_input_artifacts import _generation_context


class GenerationRegistryTests(unittest.TestCase):
    def test_native_registration_requires_occurrence_atomic_seal_path(
        self,
    ) -> None:
        from scheduler.generation_registry import (
            register_native_generation,
        )

        context = _generation_context()
        with (
            patch(
                "scheduler.generation_registry.open_native_generation",
            ) as opener,
            self.assertRaisesRegex(
                ValueError,
                "occurrence_id is required",
            ),
        ):
            register_native_generation(
                object(),
                context,
            )

        opener.assert_not_called()

    def test_databridge_registration_requires_occurrence_atomic_seal_path(
        self,
    ) -> None:
        from scheduler.generation_registry import (
            register_databridge_generation,
        )
        from shared.databridge_input_generation import (
            create_databridge_generation,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            context = create_databridge_generation(
                _current_dataset(),
                native_generation=_native_cutoff_context(),
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=Path(tmpdir),
                schema_path=SCHEMA_PATH,
            )
            with (
                patch(
                    "scheduler.generation_registry."
                    "open_databridge_generation",
                ) as opener,
                self.assertRaisesRegex(
                    ValueError,
                    "occurrence_id is required",
                ),
            ):
                register_databridge_generation(
                    object(),
                    context,
                    schema_path=SCHEMA_PATH,
                )

        opener.assert_not_called()

    def test_occurrence_registration_seals_and_binds_in_one_transaction(
        self,
    ) -> None:
        from scheduler.generation_registry import (
            register_native_generation,
        )

        context = _generation_context()
        with (
            patch(
                "scheduler.generation_registry.open_native_generation",
                return_value=context,
            ),
            patch(
                "scheduler.generation_registry."
                "register_seal_and_bind_schedule_occurrence_generation",
                return_value=(context.generation_id, 17),
            ) as atomic_register,
        ):
            generation_id = register_native_generation(
                object(),
                context,
                occurrence_id=42,
            )

        self.assertEqual(generation_id, context.generation_id)
        atomic_register.assert_called_once()
        self.assertEqual(
            atomic_register.call_args.kwargs["occurrence_id"],
            42,
        )
        self.assertEqual(
            atomic_register.call_args.kwargs["expected_feature_date"],
            context.feature_date,
        )
        self.assertEqual(
            atomic_register.call_args.kwargs["generation_id"],
            context.generation_id,
        )
        self.assertNotIn(
            "sealed_at",
            atomic_register.call_args.kwargs,
        )

    def test_databridge_registration_rehashes_native_parent_from_db_fence(
        self,
    ) -> None:
        from scheduler.generation_registry import (
            register_databridge_generation,
        )
        from shared.databridge_input_generation import (
            create_databridge_generation,
        )

        native = _native_cutoff_context()
        with tempfile.TemporaryDirectory() as tmpdir:
            context = create_databridge_generation(
                _current_dataset(),
                native_generation=native,
                business_date="2026-07-24",
                feature_date="2026-07-23",
                output_root=Path(tmpdir),
                schema_path=SCHEMA_PATH,
            )
            parent_fence = SimpleNamespace(
                generation_id=native.generation_id,
                generation_type="native_source",
                manifest_uri=str(native.manifest_path),
                manifest_sha256=native.manifest_sha256,
                business_date=native.business_date,
                feature_date=native.feature_date,
                state="SEALED",
            )
            with (
                patch(
                    "scheduler.generation_registry."
                    "read_sealed_input_generation",
                    return_value=parent_fence,
                ) as read_parent,
                patch(
                    "scheduler.generation_registry."
                    "open_native_generation",
                    return_value=native,
                ) as open_parent,
                patch(
                    "scheduler.generation_registry."
                    "register_seal_and_bind_schedule_occurrence_generation",
                    return_value=(context.generation_id, 4),
                ) as atomic_register,
            ):
                generation_id = register_databridge_generation(
                    object(),
                    context,
                    schema_path=SCHEMA_PATH,
                    occurrence_id=42,
                )

        self.assertEqual(generation_id, context.generation_id)
        read_parent.assert_called_once_with(
            atomic_register.call_args.args[0],
            generation_id=native.generation_id,
            expected_generation_type="native_source",
        )
        open_parent.assert_called_once_with(
            native.manifest_path,
            expected_generation_id=native.generation_id,
            expected_manifest_sha256=native.manifest_sha256,
            expected_business_date=native.business_date,
            expected_feature_date=native.feature_date,
        )
        atomic_register.assert_called_once()


if __name__ == "__main__":
    unittest.main()
