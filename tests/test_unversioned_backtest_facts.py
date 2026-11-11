from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.unversioned_backtest_facts import load_unversioned_backtest_facts


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ENTRY = {
    "scheme_id": "legacy_scheme",
    "target_tenor": "5Y",
    "horizon": 5,
    "expected_fact_count": 3,
    "facts_sha256": "a" * 64,
    "reason": "fixture",
}


def _write(root: Path, payload: object) -> Path:
    (root / "deploy").mkdir()
    (root / "deploy" / "unversioned_backtest_facts_v1.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return root


def _doc(*entries: dict) -> dict:
    return {"schema_version": "unversioned-backtest-facts-v1", "entries": list(entries)}


def test_shipped_inventory_loads_and_is_internally_consistent() -> None:
    entries = load_unversioned_backtest_facts(PROJECT_ROOT)

    assert entries
    identities = {(e.scheme_id, e.target_tenor, e.horizon) for e in entries}
    assert len(identities) == len(entries)


def test_valid_inventory_loads(tmp_path: Path) -> None:
    entries = load_unversioned_backtest_facts(_write(tmp_path, _doc(_ENTRY)))

    assert {e.scheme_id for e in entries} == {"legacy_scheme"}


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": "wrong", "entries": []},
        {"entries": []},
        _doc({**_ENTRY, "extra": 1}),
        _doc({**_ENTRY, "facts_sha256": "xyz"}),
        _doc({**_ENTRY, "expected_fact_count": 0}),
        _doc({**_ENTRY, "horizon": True}),
        _doc({**_ENTRY, "target_tenor": "5"}),
        _doc({**_ENTRY, "reason": " padded "}),
        _doc(_ENTRY, {**_ENTRY, "facts_sha256": "b" * 64}),
    ],
)
def test_invalid_inventory_is_rejected(tmp_path: Path, payload: object) -> None:
    with pytest.raises(ValueError):
        load_unversioned_backtest_facts(_write(tmp_path, payload))
