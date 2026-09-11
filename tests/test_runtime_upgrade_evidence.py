"""迁移期身份转换证明；随临时迁移入口一起删除。"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from harness.runtime_upgrade_evidence import (
    rebind_reviewed_w3b_state_metadata,
    verify_identity_only_delivery_change,
)


ROOT = Path(__file__).resolve().parents[1]
BASE = "liwei_0616_10y02_cons_say_k3_div_k5"


def _payloads() -> dict[str, object]:
    source = (
        ROOT / "schemes" / (BASE + "_bbv2") / "delivery"
        / (BASE + "_bbv2.json")
    ).read_bytes()
    candidate = json.loads(source)
    candidate["scheme_id"] = BASE
    return {
        "project_root": ROOT,
        "source_script": b"def predict():\n    return 1\n",
        "candidate_script": b"def predict():\n    return 1\n",
        "source_metadata": source,
        "candidate_metadata": json.dumps(candidate).encode(),
    }


def test_identity_proof_preserves_algorithm_and_both_metadata_hashes() -> None:
    payloads = _payloads()
    proof = verify_identity_only_delivery_change(**payloads)
    assert proof["scheme_id"] == BASE
    assert proof["source_scheme_id"] == BASE + "_bbv2"
    assert proof["source_code_sha256"] == proof["candidate_code_sha256"]
    assert proof["candidate_metadata_sha256"] == hashlib.sha256(
        payloads["candidate_metadata"]
    ).hexdigest()
    assert proof["source_metadata_sha256"] != proof["candidate_metadata_sha256"]
    assert proof["algorithm_executions"] == 0


@pytest.mark.parametrize("field,value", [
    ("algorithm_version", "changed"),
    ("owner", "different-owner"),
    ("description", "changed description"),
])
def test_identity_proof_rejects_other_metadata_changes(field, value) -> None:
    payloads = _payloads()
    candidate = json.loads(payloads["candidate_metadata"])
    candidate[field] = value
    payloads["candidate_metadata"] = json.dumps(candidate).encode()
    with pytest.raises(ValueError, match="only Metadata scheme_id"):
        verify_identity_only_delivery_change(**payloads)


def test_identity_proof_rejects_changed_algorithm_even_comment() -> None:
    payloads = _payloads()
    payloads["candidate_script"] += b"# changed\n"
    with pytest.raises(ValueError, match="exact algorithm bytes"):
        verify_identity_only_delivery_change(**payloads)


def test_identity_proof_rejects_unapproved_identity() -> None:
    payloads = _payloads()
    candidate = json.loads(payloads["candidate_metadata"])
    candidate["scheme_id"] = "unapproved"
    payloads["candidate_metadata"] = json.dumps(candidate).encode()
    with pytest.raises(ValueError, match="approved migration mapping"):
        verify_identity_only_delivery_change(**payloads)


def test_identity_proof_rejects_unsafe_unchanged_algorithm() -> None:
    payloads = _payloads()
    payloads["source_script"] = payloads["candidate_script"] = b"import subprocess\n"
    with pytest.raises(ValueError, match="unsafe converted algorithm"):
        verify_identity_only_delivery_change(**payloads)


def test_identity_proof_rejects_duplicate_metadata_keys() -> None:
    payloads = _payloads()
    payloads["candidate_metadata"] = b'{"scheme_id":"a","scheme_id":"b"}'
    with pytest.raises(ValueError):
        verify_identity_only_delivery_change(**payloads)


def _state_fixture():
    import numpy as np

    payloads = _payloads()
    script = (
        ROOT / "schemes" / (BASE + "_bbv2") / "delivery" / (BASE + "_bbv2.py")
    ).read_bytes()
    payloads["source_script"] = payloads["candidate_script"] = script
    proof = verify_identity_only_delivery_change(**payloads)
    identity = {
        "code": proof["source_code_sha256"],
        "metadata": proof["source_metadata_sha256"],
        "machine": "arm64",
        "schema": "10y02-full-oos-private-1",
    }
    header = {"identity": identity, "cutoff": "2026-09-08", "input_prefixes": {}}
    arrays = {
        f"{family}_{field}": np.array([1, 2, 3])
        for family in ("ten_y", "seven_y")
        for field in ("dates", "features", "preds", "probs")
    }
    buffer = io.BytesIO()
    np.savez(buffer, header=np.array(json.dumps(header)), **arrays)
    source = buffer.getvalue()
    return {
        "source_state": source,
        "source_metadata": payloads["source_metadata"],
        "candidate_metadata": payloads["candidate_metadata"],
        "expected_state_sha256": hashlib.sha256(source).hexdigest(),
        "expected_algorithm_identity": identity,
        "identity_conversion": proof,
    }


def test_state_rebinding_changes_only_metadata_and_preserves_npy_bytes() -> None:
    import numpy as np

    inputs = _state_fixture()
    converted, receipt = rebind_reviewed_w3b_state_metadata(**inputs)
    with (
        zipfile.ZipFile(io.BytesIO(inputs["source_state"])) as original,
        zipfile.ZipFile(io.BytesIO(converted)) as result,
    ):
        assert original.namelist() == result.namelist()
        for name in original.namelist():
            if name != "header.npy":
                assert original.read(name) == result.read(name)
        old_header = json.loads(str(np.load(io.BytesIO(original.read("header.npy"))).item()))
        new_header = json.loads(str(np.load(io.BytesIO(result.read("header.npy"))).item()))
        old_header["identity"]["metadata"] = inputs["identity_conversion"]["candidate_metadata_sha256"]
        assert new_header == old_header
    assert receipt["algorithm_executions"] == 0
    assert receipt["converted_state_sha256"] == hashlib.sha256(converted).hexdigest()
    assert len(receipt["unchanged_arrays_sha256"]) == 8


@pytest.mark.parametrize("change", ["digest", "environment", "code", "candidate_metadata", "source_id"])
def test_state_rebinding_rejects_unverified_source(change) -> None:
    inputs = _state_fixture()
    if change == "digest":
        inputs["expected_state_sha256"] = "0" * 64
    elif change == "environment":
        inputs["expected_algorithm_identity"]["machine"] = "x86_64"
    elif change == "code":
        inputs["identity_conversion"]["candidate_code_sha256"] = "0" * 64
    elif change == "candidate_metadata":
        inputs["identity_conversion"]["candidate_metadata_sha256"] = "0" * 64
    else:
        inputs["identity_conversion"]["source_scheme_id"] = "unapproved_source"
    with pytest.raises(ValueError):
        rebind_reviewed_w3b_state_metadata(**inputs)
