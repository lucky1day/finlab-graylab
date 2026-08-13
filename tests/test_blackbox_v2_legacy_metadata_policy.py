from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write(root: Path, payload: object) -> None:
    deploy = root / "deploy"
    deploy.mkdir(parents=True, exist_ok=True)
    (deploy / "blackbox_v2_legacy_metadata_v1.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_repository_policy_exactly_matches_current_ownerless_blackbox_metadata() -> None:
    from scheduler.discovery import discover_schemes
    from shared.blackbox_v2.legacy_metadata_policy import (
        load_legacy_metadata_hashes,
    )

    root = Path(__file__).resolve().parents[1]
    ownerless = {
        config.scheme_id: config.manifest_hash
        for config in discover_schemes(root / "schemes", strict=True)
        if config.runtime_type == "blackbox_v2" and config.owner is None
    }

    assert load_legacy_metadata_hashes(root) == ownerless


def test_policy_fails_closed_on_unsorted_or_duplicate_ids(tmp_path: Path) -> None:
    from shared.blackbox_v2.legacy_metadata_policy import (
        LegacyMetadataPolicyError,
        load_legacy_metadata_hashes,
    )

    for metadata_hashes in (
        {"z_scheme": "a" * 64, "a_scheme": "b" * 64},
        {"a_scheme": "not-a-sha256"},
    ):
        _write(
            tmp_path,
            {
                "schema_version": "blackbox-v2-legacy-metadata-v1",
                "metadata_sha256": metadata_hashes,
            },
        )
        with pytest.raises(LegacyMetadataPolicyError):
            load_legacy_metadata_hashes(tmp_path)
