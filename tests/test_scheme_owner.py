"""方案归属映射的读取契约。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.factor_lab_dashboard import _registry_dto
from backend.scheme_owner import SchemeOwnerError, load_scheme_owners
from shared.scheme_owner_registry import owner_registry_scheme_id


def _write(root: Path, payload: object) -> None:
    target = root / "deploy"
    target.mkdir(parents=True, exist_ok=True)
    (target / "scheme_owner_v1.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_reads_composite_scheme_id_mapping(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "schema_version": "scheme-owner-v1",
            "owners": {"demo__h1__10Y": "LW"},
        },
    )

    assert load_scheme_owners(tmp_path) == {"demo__h1__10Y": "LW"}


def test_builds_canonical_composite_scheme_id() -> None:
    assert owner_registry_scheme_id("demo", 5, "10Y") == "demo__h5__10Y"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {
            "schema_version": "scheme-owner-v1",
            "owners": {"demo__h1__10Y": "unknown"},
        },
    ],
    ids=("missing", "placeholder"),
)
def test_missing_or_placeholder_owner_fails_closed(
    tmp_path: Path,
    payload: object | None,
) -> None:
    if payload is not None:
        _write(tmp_path, payload)

    with pytest.raises(SchemeOwnerError):
        load_scheme_owners(tmp_path)


def test_repository_file_is_loadable() -> None:
    """仓库中的实际文件必须始终可读，否则 dashboard 会整体 fail-closed。"""
    assert isinstance(load_scheme_owners(), dict)


def test_dashboard_owner_lookup_uses_composite_scheme_id() -> None:
    row = {
        "scheme_id": "demo__h1__10Y",
        "base_scheme_id": "demo",
        "name": "Demo",
        "description": "Demo scheme",
        "horizon": 1,
        "task_type": "T+1",
        "frequency": "daily",
        "target_tenor": "10Y",
        "status": "active",
        "deployed_at": "2026-01-01",
    }

    assert _registry_dto(row, {"demo__h1__10Y": "LW"})["owner"] == "LW"
    assert _registry_dto(row, {"demo": "LW"})["owner"] == ""
