"""日批原子提交前的本机文件与方案版本复核。"""

from __future__ import annotations

import re
from pathlib import Path

from scheduler.discovery import load_scheme_config
from scheduler.repository import (
    ScheduledCompletionEvidence,
    ScheduledCompletionExpectation,
)
from shared.databridge_input_generation import (
    open_databridge_generation,
)
from shared.native_input_generation import open_native_generation


_SCHEME_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class ScheduledVerificationError(RuntimeError):
    """冻结 generation 或方案代码与 occurrence 期望不一致。"""


class FilesystemScheduledCompletionVerifier:
    """在 ledger 事务外重新读取实际文件并返回强类型观测证据。"""

    def __init__(
        self,
        *,
        project_root: str | Path,
        databridge_schema_path: str | Path,
    ) -> None:
        root = Path(project_root)
        if not root.is_absolute():
            raise ValueError("project_root must be absolute")
        self._project_root = root.resolve()
        self._schemes_root = self._project_root / "schemes"
        schema = Path(databridge_schema_path)
        if not schema.is_absolute():
            raise ValueError("databridge_schema_path must be absolute")
        self._databridge_schema_path = schema.resolve(strict=False)

    def verify(
        self,
        expectation: ScheduledCompletionExpectation,
    ) -> ScheduledCompletionEvidence:
        """重开 generation、重算代码/config/version，漂移时 fail-closed。"""
        if not isinstance(expectation, ScheduledCompletionExpectation):
            raise TypeError(
                "expectation must be ScheduledCompletionExpectation"
            )
        scheme_id = expectation.base_scheme_id
        if _SCHEME_ID.fullmatch(scheme_id) is None:
            raise ScheduledVerificationError(
                f"unsafe base_scheme_id: {scheme_id!r}"
            )
        if expectation.runtime_type not in {
            "native_adapter",
            "blackbox_v2",
        }:
            raise ScheduledVerificationError(
                f"unsupported runtime_type: {expectation.runtime_type}"
            )
        manifest_path = Path(expectation.manifest_uri)
        if (
            not manifest_path.is_absolute()
            or manifest_path.name != "manifest.json"
        ):
            raise ScheduledVerificationError(
                "generation manifest_uri must be an absolute manifest.json"
            )

        native_generation = None
        try:
            if expectation.runtime_type == "native_adapter":
                generation = open_native_generation(
                    manifest_path,
                    expected_generation_id=expectation.generation_id,
                    expected_manifest_sha256=expectation.manifest_sha256,
                    expected_business_date=expectation.business_date,
                    expected_feature_date=expectation.feature_date,
                )
            else:
                linked_values = {
                    "native_generation_id":
                        expectation.native_generation_id,
                    "native_manifest_uri":
                        expectation.native_manifest_uri,
                    "native_manifest_sha256":
                        expectation.native_manifest_sha256,
                    "native_feature_date":
                        expectation.native_feature_date,
                    "native_business_date":
                        expectation.native_business_date,
                }
                missing_linked = sorted(
                    field
                    for field, value in linked_values.items()
                    if value is None or not str(value).strip()
                )
                if missing_linked:
                    raise ValueError(
                        "linked Native generation expectation is missing: "
                        + ", ".join(missing_linked)
                    )
                generation = open_databridge_generation(
                    manifest_path,
                    expected_generation_id=expectation.generation_id,
                    expected_manifest_sha256=expectation.manifest_sha256,
                    expected_business_date=expectation.business_date,
                    expected_feature_date=expectation.feature_date,
                    schema_path=self._databridge_schema_path,
                )
                if (
                    str(generation.native_generation_id)
                    != str(expectation.native_generation_id)
                    or str(generation.native_manifest_sha256)
                    != str(expectation.native_manifest_sha256)
                ):
                    raise ValueError(
                        "DataBridge linked Native generation identity drifted"
                    )
                native_manifest_path = Path(
                    str(expectation.native_manifest_uri)
                )
                if (
                    not native_manifest_path.is_absolute()
                    or native_manifest_path.name != "manifest.json"
                ):
                    raise ValueError(
                        "linked Native manifest_uri must be an absolute "
                        "manifest.json"
                    )
                native_generation = open_native_generation(
                    native_manifest_path,
                    expected_generation_id=str(
                        expectation.native_generation_id
                    ),
                    expected_manifest_sha256=str(
                        expectation.native_manifest_sha256
                    ),
                    expected_business_date=str(
                        expectation.native_business_date
                    ),
                    expected_feature_date=str(
                        expectation.native_feature_date
                    ),
                )
        except Exception as exc:
            raise ScheduledVerificationError(
                f"generation verification failed: {exc}"
            ) from exc

        config_path = (
            self._schemes_root / scheme_id / "config.yaml"
        ).resolve(strict=False)
        scheme_directory = config_path.parent
        if (
            self._schemes_root not in scheme_directory.parents
            or config_path.name != "config.yaml"
        ):
            raise ScheduledVerificationError(
                "scheme config path escaped schemes root"
            )
        try:
            config = load_scheme_config(config_path)
        except Exception as exc:
            raise ScheduledVerificationError(
                f"scheme config verification failed: {exc}"
            ) from exc

        actual = {
            "observed_generation_id": str(generation.generation_id),
            "manifest_sha256": str(generation.manifest_sha256),
            "generation_dataset_content_id": str(
                generation.dataset_content_id
            ),
            "generation_schema_version": str(generation.schema_version),
            "generation_exporter_version": str(
                generation.exporter_version
            ),
            "feature_date": str(generation.feature_date),
            "scheme_version": str(config.scheme_version),
            "code_sha256": str(config.code_hash),
            "config_sha256": str(config.config_hash),
        }
        expected = {
            "observed_generation_id": expectation.generation_id,
            "manifest_sha256": expectation.manifest_sha256,
            "generation_dataset_content_id":
                expectation.generation_dataset_content_id,
            "generation_schema_version":
                expectation.generation_schema_version,
            "generation_exporter_version":
                expectation.generation_exporter_version,
            "feature_date": expectation.feature_date,
            "scheme_version": expectation.scheme_version,
            "code_sha256": expectation.code_sha256,
            "config_sha256": expectation.config_sha256,
        }
        native_actual = {
            "native_generation_id": (
                str(native_generation.generation_id)
                if native_generation is not None
                else None
            ),
            "native_manifest_sha256": (
                str(native_generation.manifest_sha256)
                if native_generation is not None
                else None
            ),
            "native_dataset_content_id": (
                str(native_generation.dataset_content_id)
                if native_generation is not None
                else None
            ),
            "native_schema_version": (
                str(native_generation.schema_version)
                if native_generation is not None
                else None
            ),
            "native_exporter_version": (
                str(native_generation.exporter_version)
                if native_generation is not None
                else None
            ),
            "native_feature_date": (
                str(native_generation.feature_date)
                if native_generation is not None
                else None
            ),
        }
        native_expected = {
            "native_generation_id": expectation.native_generation_id,
            "native_manifest_sha256":
                expectation.native_manifest_sha256,
            "native_dataset_content_id":
                expectation.native_dataset_content_id,
            "native_schema_version":
                expectation.native_schema_version,
            "native_exporter_version":
                expectation.native_exporter_version,
            "native_feature_date": expectation.native_feature_date,
        }
        drift = sorted(
            field
            for field, value in actual.items()
            if value != expected[field]
        )
        if config.scheme_id != scheme_id:
            drift.append("scheme_id")
        if config.runtime_type != expectation.runtime_type:
            drift.append("runtime_type")
        drift.extend(
            field
            for field, value in native_actual.items()
            if value != native_expected[field]
        )
        if drift:
            raise ScheduledVerificationError(
                "scheduled completion evidence drift: "
                + ", ".join(sorted(set(drift)))
            )
        return ScheduledCompletionEvidence(
            **actual,
            **native_actual,
        )
