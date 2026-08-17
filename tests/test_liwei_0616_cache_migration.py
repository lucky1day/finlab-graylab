from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable
from typing import Any

import pytest

from shared.liwei_0616_cache_contract import canonical_json_bytes
from shared.liwei_0616_cache_migration import (
    active_cache_rebind_entry,
    authorized_cache_rebind,
    validate_cache_rebind_receipt,
)


def _baseline_evidence(seed: str) -> dict[str, Any]:
    return {
        "cache_content_sha256": seed * 64,
        "field_sha256": {
            "test_dates": "a" * 64,
            "results[].config": "b" * 64,
            "results[].preds": "c" * 64,
            "results[].probs": "d" * 64,
        },
        "test_date_count": 2,
        "result_config_count": 1,
    }


def _entry(index: int) -> dict[str, Any]:
    value = {
        "cache_family": f"family-{index}",
        "tenor": f"{index + 1}Y",
        "publisher_consumer_id": f"publisher-{index}",
        "spec_fingerprint": "1" * 64,
        "parent_generation_id": f"generation-parent-{index}",
        "parent_manifest_sha256": "2" * 64,
        "parent_generation_content_id": "3" * 64,
        "parent_input_content_id": "4" * 64,
        "target_input_content_id": "5" * 64,
        "baselines": {
            f"baseline-{index}": _baseline_evidence("6"),
        },
    }
    value["entry_sha256"] = hashlib.sha256(
        canonical_json_bytes(value)
    ).hexdigest()
    return value


def _receipt() -> dict[str, Any]:
    value = {
        "schema_version": "liwei-0616-migration-rebind-v1",
        "migration_id": "aliyun-linux-x86_64-20260817-v1",
        "entries": [_entry(index) for index in range(7)],
    }
    value["receipt_sha256"] = hashlib.sha256(
        canonical_json_bytes(value)
    ).hexdigest()
    return value


def _refresh_entry_and_receipt(
    receipt: dict[str, Any],
    index: int,
) -> None:
    entry = receipt["entries"][index]
    entry_payload = {
        key: value
        for key, value in entry.items()
        if key != "entry_sha256"
    }
    entry["entry_sha256"] = hashlib.sha256(
        canonical_json_bytes(entry_payload)
    ).hexdigest()
    receipt_payload = {
        key: value
        for key, value in receipt.items()
        if key != "receipt_sha256"
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        canonical_json_bytes(receipt_payload)
    ).hexdigest()


def _duplicate_entry(receipt: dict[str, Any]) -> None:
    receipt["entries"][-1] = copy.deepcopy(receipt["entries"][0])
    _refresh_entry_and_receipt(receipt, 0)


def _invalidate_target_sha(receipt: dict[str, Any]) -> None:
    receipt["entries"][0]["target_input_content_id"] = "bad"
    _refresh_entry_and_receipt(receipt, 0)


def _invalidate_receipt_digest(receipt: dict[str, Any]) -> None:
    receipt["receipt_sha256"] = "0" * 64


def test_validate_rebind_receipt_round_trips_exact_record() -> None:
    receipt = _receipt()

    assert validate_cache_rebind_receipt(receipt) == receipt


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (_duplicate_entry, "duplicate"),
        (_invalidate_target_sha, "sha256"),
        (_invalidate_receipt_digest, "digest"),
    ],
)
def test_validate_rebind_receipt_rejects_mutation(
    mutation: Callable[[dict[str, Any]], None],
    match: str,
) -> None:
    receipt = _receipt()
    mutation(receipt)

    with pytest.raises(ValueError, match=match):
        validate_cache_rebind_receipt(receipt)


def test_authorized_cache_rebind_is_process_local_and_exact() -> None:
    assert active_cache_rebind_entry(
        cache_family="family-0",
        tenor="1Y",
        publisher_consumer_id="publisher-0",
    ) is None

    with authorized_cache_rebind(_receipt()):
        authorization = active_cache_rebind_entry(
            cache_family="family-0",
            tenor="1Y",
            publisher_consumer_id="publisher-0",
        )
        assert authorization is not None
        assert (
            authorization["entry"]["target_input_content_id"]
            == "5" * 64
        )
        assert active_cache_rebind_entry(
            cache_family="family-0",
            tenor="1Y",
            publisher_consumer_id="consumer-0",
        ) is None

    assert active_cache_rebind_entry(
        cache_family="family-0",
        tenor="1Y",
        publisher_consumer_id="publisher-0",
    ) is None


def test_authorized_cache_rebind_rejects_nested_context() -> None:
    with authorized_cache_rebind(_receipt()):
        with pytest.raises(RuntimeError, match="already active"):
            with authorized_cache_rebind(_receipt()):
                raise AssertionError("unreachable")
