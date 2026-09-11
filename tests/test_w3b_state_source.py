"""临时 W3B 固定来源加载边界；不执行算法、不访问生产状态或数据库。"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from harness import w3b_state_source as source
from scheduler.blackbox_state import _encode
from scheduler.discovery import load_scheme_config


ROOT = Path(__file__).resolve().parents[1]
K5 = "liwei_0616_10y02_cons_say_k3_div_k5_bbv2"


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    """合成小型已认证原件，测试摘要封闭与身份绑定而非算法内部。"""
    evidence = tmp_path / "evidence"
    runtime = tmp_path / "runtime"
    evidence.mkdir()
    monkeypatch.setattr(source, "_EVIDENCE_ROOT", evidence)
    monkeypatch.setenv("BFL_RUNTIME_ROOT", str(runtime))
    specs = {key: dict(value) for key, value in source._SOURCES.items()}
    monkeypatch.setattr(source, "_SOURCES", specs)

    def write(relative, value):
        path = evidence / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = value if isinstance(value, bytes) else json.dumps(value).encode()
        path.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    controller_sha = write("initialize_w3b_ecs_state.py", b"initializer")
    monkeypatch.setattr(source, "_INITIALIZER_SHA", controller_sha)
    monkeypatch.setattr(source, "_SAY_DRIVER_SHA", write("simulate_adopted_say_state.py", b"simulation"))
    monkeypatch.setattr(source, "_ADOPTION_DRIVER_SHA", write("adopt_reviewed_say_state.py", b"adoption"))
    items = {}
    for scheme_id in (source._FULL, K5, source._SAY):
        cfg = load_scheme_config(ROOT / "schemes" / scheme_id / "config.yaml")
        request = {"request_id": scheme_id + ":2026-09-11:2026-09-10:2026-09-17",
                   "predict_date": "2026-09-11", "feature_date": "2026-09-10", "target_date": "2026-09-17",
                   "daily_cutoff_key": "2026-09-10", "weekly_cutoff_key": "202635", "monthly_cutoff_key": "202609"}
        result = {key: request[key] for key in ("request_id", "predict_date", "feature_date", "target_date")}
        result["predicted_direction"] = 1
        algorithm_identity = {"code": cfg.code_hash, "metadata": cfg.manifest_hash, "machine": "x86_64"}
        buffer = io.BytesIO()
        np.savez(buffer, header=np.array(json.dumps({"identity": algorithm_identity, "cutoff": request["feature_date"]})))
        payload = buffer.getvalue()
        header = {"identity": {"scheme_id": scheme_id, "scheme_version": cfg.scheme_version,
                               "script_sha256": cfg.code_hash, "manifest_sha256": cfg.manifest_hash},
                  "input": {"schema": "data-bridge-v1", "generation_id": "frozen-generation", "snapshot_id": "frozen-snapshot", "files": {}},
                  "payload_sha256": source._sha(payload)}
        envelope = _encode(header, payload)
        state_path = runtime / "blackbox-state" / scheme_id / (cfg.scheme_version + ".state")
        state_path.parent.mkdir(parents=True)
        state_path.write_bytes(envelope)
        spec = specs[scheme_id]
        if scheme_id != source._SAY:
            plan = {"driver_sha256": controller_sha, "request": request,
                    "generation_id": "frozen-generation", "snapshot_id": "frozen-snapshot",
                    "state_destination": str(state_path), "expected_state_identity": header["identity"],
                    "expected_state_input": header["input"]}
            started = {"plan": plan, "plan_sha256": source._plan_sha(plan)}
            report = {"result": result, "state": {"payload_sha256": source._sha(payload), "envelope_sha256": source._sha(envelope)},
                      "state_audit": {"state_input_sha256": source._sha(payload)}}
            complete = {"plan_sha256": started["plan_sha256"], "results": {"cold": report, "warm": report},
                        "algorithm_executions": 2, "target_scheme_facts_unchanged": True, "prediction_written": False}
            spec["started"] = write(spec["directory"] + "/started.json", started)
            spec["complete"] = write(spec["directory"] + "/complete.json", complete)
        else:
            adoption_plan = {"destination": str(state_path)}
            adoption_started = {"plan": adoption_plan, "plan_sha256": source._plan_sha(adoption_plan)}
            adopted = {"plan_sha256": adoption_started["plan_sha256"], "status": "passed", "algorithm_executions": 0,
                       "state": {"state_envelope_sha256": "old-envelope", "state_output_sha256": "old-payload"},
                       "envelope_header": {"identity": header["identity"]}}
            started = {"adoption_plan_sha256": adopted["plan_sha256"], "driver_sha256": source._SAY_DRIVER_SHA,
                       "before_envelope_sha256": "old-envelope", "before_facts": {},
                       "ready_generation_id": "frozen-generation", "ready_snapshot_id": "frozen-snapshot"}
            complete = {"status": "passed", "algorithm_executions": 1, "prediction_written": False,
                        "after_facts": {}, "result": result, "envelope_header": header,
                        "state": {"state_input_sha256": "old-payload", "state_output_sha256": source._sha(payload),
                                  "state_envelope_sha256": source._sha(envelope)}}
            monkeypatch.setattr(source, "_ADOPTION_STARTED_SHA", write("adopt-reviewed-say-state/started.json", adoption_started))
            monkeypatch.setattr(source, "_ADOPTION_COMPLETE_SHA", write("adopt-reviewed-say-state/completed.json", adopted))
            spec["started"] = write(spec["directory"] + "/started.json", started)
            spec["complete"] = write(spec["directory"] + "/completed.json", complete)
        items[scheme_id] = {"cfg": cfg, "state_path": state_path, "envelope": envelope, "identity": algorithm_identity,
                            "complete": complete, "started": started, "spec": spec}
    return items, write


@pytest.mark.parametrize("scheme_id", [source._FULL, K5, source._SAY])
def test_loads_authenticated_source_without_modifying_state(fixture, scheme_id):
    items, _ = fixture
    item = items[scheme_id]
    result = source.load_reviewed_w3b_state_source(item["cfg"])
    assert result["result"].predicted_direction == 1
    assert result["expected_algorithm_identity"] == item["identity"]
    assert result["source_envelope_sha256"] == source._sha(item["envelope"])
    assert item["state_path"].read_bytes() == item["envelope"]
    assert result["source_evidence"]["request_reconstructed"] is (scheme_id == source._SAY)
    assert result["source_evidence"]["publication_authorized"] is False
    assert result["request"].weekly_cutoff_key == "202635"
    json.dumps(result["source_evidence"])


@pytest.mark.parametrize("change", ["unknown", "version", "config_hash", "controller", "receipt", "state", "root", "symlink", "failure"])
def test_rejects_unapproved_or_changed_source(fixture, monkeypatch, tmp_path, change):
    items, _ = fixture
    item = items[K5]
    cfg = item["cfg"]
    if change == "unknown":
        cfg = replace(cfg, scheme_id="unapproved")
    elif change in ("version", "config_hash"):
        cfg = replace(cfg, **{"scheme_version" if change == "version" else change: "changed"})
    elif change == "controller":
        (source._EVIDENCE_ROOT / "initialize_w3b_ecs_state.py").write_bytes(b"changed")
    elif change == "receipt":
        (source._EVIDENCE_ROOT / item["spec"]["directory"] / "complete.json").write_text('{"status":"passed"}')
    elif change == "state":
        item["state_path"].write_bytes(item["envelope"] + b"changed")
    elif change == "root":
        monkeypatch.setenv("BFL_RUNTIME_ROOT", str(tmp_path / "other"))
    elif change == "failure":
        (source._EVIDENCE_ROOT / item["spec"]["directory"] / "failure.json").write_text("{}")
    else:
        target = item["state_path"].with_suffix(".original")
        item["state_path"].rename(target)
        item["state_path"].symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        source.load_reviewed_w3b_state_source(cfg)


@pytest.mark.parametrize("change", ["snapshot", "dates", "echo", "input"])
def test_rejects_inconsistent_authenticated_receipt_fields(fixture, change):
    items, write = fixture
    item = items[source._SAY]
    complete = item["complete"]
    if change == "snapshot":
        complete["envelope_header"]["input"]["snapshot_id"] = "different-snapshot"
    elif change == "dates":
        complete["result"]["feature_date"] = "2026-09-09"
    elif change == "echo":
        complete["result"]["request_id"] = "unapproved-request"
    else:
        complete["envelope_header"]["input"]["files"] = {"daily_output.csv": "changed"}
    item["spec"]["complete"] = write(item["spec"]["directory"] + "/completed.json", complete)
    with pytest.raises(ValueError):
        source.load_reviewed_w3b_state_source(item["cfg"])


@pytest.mark.parametrize("directory", ["adopt-reviewed-say-state", "simulate-adopted-say-state-20260911"])
def test_say_rejects_failure_in_either_ancestry_stage(fixture, directory):
    items, _ = fixture
    (source._EVIDENCE_ROOT / directory / "failed.json").write_text("{}")
    with pytest.raises(ValueError, match="failure receipt"):
        source.load_reviewed_w3b_state_source(items[source._SAY]["cfg"])
