"""W3B 原件复用的一次开发验收；不执行算法、联网或访问数据库。"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import native_successor_migration as migration


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "outputs"
LOCAL_INPUT = OUTPUTS / "w3b-k5-local-20260911.Ip7Jlr"
LOCAL_STAGED = OUTPUTS / "releases/w3b-staged-reference-20260911"


@dataclass
class Candidate:
    scheme_id: str
    runtime_profile: str
    code_hash: str
    delivery_script: Path
    delivery_metadata: Path
    environment_fingerprint: str = ""
    incremental_state: bool = True


@pytest.fixture
def captured(tmp_path, monkeypatch):
    """保留真实原件摘要，只把已捕获 ECS 路径映射到本机私有副本。"""
    if not (LOCAL_INPUT / "say-requests.csv").is_file():
        pytest.skip("private W3B captured evidence is not available")
    wave = migration.load_native_successor_waves(ROOT / "deploy/native_to_blackbox_migration_v1.json")["W3B"]
    originals = {
        "say": OUTPUTS / "releases/w3b-weeklyfix-20260910/full-comparison-execution",
        "full": LOCAL_STAGED / "model-reuse-full-execution",
        "k5": LOCAL_STAGED / "model-reuse-k5-execution",
    }
    drafts = {
        "say": "native-migration-w3b-weeklyfix-draft",
        "full": "native-migration-w3b-model-reuse-draft",
        "k5": "native-migration-w3b-k5-model-reuse-draft",
    }
    entries, configs, paths = [], {}, {}
    data = tmp_path / "data"
    shutil.copytree(LOCAL_INPUT / "data", data)
    for target in wave.targets:
        approval = migration._REVIEWED_W3B_EVIDENCE[target.old_base_scheme_id]
        family = approval["family"]
        evidence = tmp_path / family
        shutil.copytree(originals[family], evidence)
        delivery = tmp_path / target.new_base_scheme_id
        shutil.copytree(OUTPUTS / drafts[family] / target.new_base_scheme_id, delivery)
        configs[target.new_base_scheme_id] = Candidate(
            scheme_id=target.new_base_scheme_id,
            runtime_profile="blackbox-v2-v1",
            code_hash=approval["code_sha256"],
            delivery_script=delivery / (target.new_base_scheme_id + ".py"),
            delivery_metadata=delivery / (target.new_base_scheme_id + ".json"),
        )
        entries.append({"old_base_scheme_id": target.old_base_scheme_id,
                        "new_base_scheme_id": target.new_base_scheme_id, "target_tenor": "10Y",
                        "evidence_dir": str(evidence), "data_dir": str(data)})
        original = Path(approval["execution_dir"])
        paths[original.parent / ("run_fixed_full_comparison.py" if family == "say" else "run_" + family + "_comparison.py")] = OUTPUTS / drafts[family] / ("run_fixed_full_comparison.py" if family == "say" else "run_" + family + "_comparison.py")
    paths[migration._W3B_SOURCE / "binding.json"] = LOCAL_INPUT / "binding.json"
    paths[migration._W3B_SOURCE / "requests.csv"] = LOCAL_INPUT / "say-requests.csv"
    for family in ("full", "k5"):
        paths[migration._W3B_STAGED / "reference-inputs" / (family + "-requests.csv")] = LOCAL_INPUT / (family + "-requests.csv")
    remaining = Path("/opt/bond-factor-lab/incoming/w3b-remaining-20260909.Aj6bv8")
    remaining_local = OUTPUTS / "releases/w3b-remaining-20260909/native-complete"
    for name in ("started.json", "native-execution.json"):
        paths[remaining / "remaining-execution" / name] = remaining_local / name
    paths[remaining / "remaining-execution/native-run/native.csv"] = remaining_local / "native.csv"
    for name in ("native_reference_v2.py", "run_remaining_comparison.py"):
        paths[remaining / name] = OUTPUTS / "native-migration-w3b-reference-draft" / name
    paths[migration._W3B_STAGED / "native_reference.py"] = OUTPUTS / "native-migration-w3b-full-reference-draft/native_reference_staged.py"
    paths[migration._W3B_STAGED / "run_family_comparison.py"] = OUTPUTS / "native-migration-w3b-full-reference-draft/run_family_comparison_staged.py"
    real_read = migration._read_reviewed_w3b_bytes

    def mapped_read(path, digest):
        local = paths.get(path, path)
        for prefix in (migration._W3B_SOURCE / "native-source", migration._W3B_STAGED / "reference-inputs/native-source"):
            if path.is_relative_to(prefix):
                local = paths.get(path, ROOT / path.relative_to(prefix))
        if local == path and path.is_relative_to(migration._W3B_STAGED):
            local = LOCAL_STAGED / path.relative_to(migration._W3B_STAGED)
        return real_read(local, digest)

    monkeypatch.setattr(migration, "_read_reviewed_w3b_bytes", mapped_read)
    load_config = migration.load_scheme_config
    def captured_config(path):
        from shared.versioning import compute_code_hash

        if path.parent.name in configs:
            return configs[path.parent.name]
        if path.parent.name in migration._REVIEWED_W3B_EVIDENCE:
            # 开发 canonical 已升级；原 Native 代码仍保留，实际计算旧闭包而非伪造批准 hash。
            return SimpleNamespace(code_hash=compute_code_hash(path.parent))
        return load_config(path)

    monkeypatch.setattr(migration, "load_scheme_config", captured_config)
    fingerprint = json.loads((ROOT / "deploy/blackbox_v2/environment_manifest.json").read_text())["environment_fingerprint"]
    monkeypatch.setattr(migration, "load_environment_fingerprint", lambda *a, **k: fingerprint)
    monkeypatch.setattr(migration, "_capture_native_runtime_identity", lambda: {"environment_fingerprint": "a" * 64})
    runtime_checked = []
    monkeypatch.setattr(migration, "_verify_reviewed_w3b_runtime", lambda *args: runtime_checked.append(args))

    def forbidden(*args, **kwargs):
        raise AssertionError("W3B reuse must not execute algorithms or use the W3A loader")

    monkeypatch.setattr(migration, "_execute_controlled_comparison", forbidden)
    monkeypatch.setattr(migration, "_load_reviewed_w3a_comparison", forbidden)
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"schema_version": "native-successor-reviewed-input-v1", "wave": "W3B", "comparisons": entries}))
    return wave, bundle, entries, configs, paths, runtime_checked


def _build(captured):
    return migration.build_native_successor_equivalence_receipt(
        project_root=ROOT, wave=captured[0], comparison_bundle_path=captured[1],
    )


def test_real_captured_three_families_produce_999_compared_rows(captured):
    receipt = _build(captured)
    assert receipt["wave"] == "W3B"
    assert len(captured[5]) == 3
    assert len(receipt["targets"]) == 3
    for target in receipt["targets"]:
        assert target["request_count"] == 333
        assert target["native_runtime_profile"] == "native"
        assert target["native_runtime_environment_fingerprint"] == "a" * 64
        assert target["native_result_sha256"] == target["successor_result_sha256"]
        assert all(value == 0 for key, value in target.items() if key.endswith("mismatch_count"))
        assert target["input_identity"]["generation_id"] == "full-20260909-063339-4688e3c69f8d"


@pytest.mark.parametrize("family", [0, 1, 2])
@pytest.mark.parametrize("name", ["comparison-report.json", "candidate.csv"])
def test_changed_report_or_result_is_rejected(captured, family, name):
    path = Path(captured[2][family]["evidence_dir"]) / name
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _build(captured)


@pytest.mark.parametrize("item", ["request", "source", "metadata", "input"])
def test_changed_closure_is_rejected(captured, tmp_path, item):
    cfg = next(iter(captured[3].values()))
    if item in {"request", "source"}:
        original = (migration._W3B_SOURCE / "requests.csv" if item == "request" else
                    migration._W3B_SOURCE / "native-source/schemes/liwei_0616_10y01_cons_say_k3_div_k10/inference.py")
        replacement = tmp_path / "changed"
        replacement.write_bytes(b"changed")
        captured[4][original] = replacement
    else:
        path = cfg.delivery_metadata if item == "metadata" else Path(captured[2][0]["data_dir"]) / "daily_output.csv"
        path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _build(captured)


def test_wrong_identity_partial_wave_and_external_fields_are_rejected(captured):
    wave, bundle = captured[:2]
    with pytest.raises(ValueError, match="complete locked"):
        migration.build_native_successor_equivalence_receipt(project_root=ROOT, wave=replace(wave, targets=wave.targets[:1]), comparison_bundle_path=bundle)
    cfg = next(iter(captured[3].values()))
    cfg.code_hash = "0" * 64
    with pytest.raises(ValueError, match="exact code identity"):
        _build(captured)
    value = json.loads(bundle.read_text())
    value["comparisons"][0]["native_result_path"] = "unapproved.csv"
    bundle.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="entry fields"):
        _build(captured)


def test_same_bytes_symlink_is_rejected(captured):
    cfg = next(iter(captured[3].values()))
    original = cfg.delivery_script
    saved = original.with_suffix(".saved")
    original.rename(saved)
    original.symlink_to(saved)
    with pytest.raises(ValueError, match="path is unsafe"):
        _build(captured)


def test_state_flag_cannot_be_removed_from_approved_successor(captured):
    next(iter(captured[3].values())).incremental_state = False
    with pytest.raises(ValueError, match="exact code identity"):
        _build(captured)


def test_report_cannot_reference_paths_outside_locked_roots(captured, monkeypatch):
    target = captured[0].targets[1]
    report_path = Path(captured[2][1]["evidence_dir"]) / "comparison-report.json"
    report = json.loads(report_path.read_text())
    report["artifacts"]["/tmp/outside"] = "0" * 64
    payload = json.dumps(report).encode()
    report_path.write_bytes(payload)
    approvals = {key: dict(value) for key, value in migration._REVIEWED_W3B_EVIDENCE.items()}
    approvals[target.old_base_scheme_id]["report_sha256"] = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(migration, "_REVIEWED_W3B_EVIDENCE", approvals)
    with pytest.raises(ValueError, match="escaped approved roots"):
        _build(captured)


def test_runtime_verification_rejects_mac_before_capture(monkeypatch, tmp_path):
    monkeypatch.setattr(migration.sys, "platform", "darwin")
    with pytest.raises(ValueError, match="captured ECS Native runtime"):
        migration._verify_reviewed_w3b_runtime({}, tmp_path)


def test_runtime_inventory_and_bytes_are_rechecked(monkeypatch, tmp_path):
    from scheduler import blackbox_v2_runner as runner

    monkeypatch.setattr(migration.sys, "platform", "linux")
    monkeypatch.setenv("BOND_ALGO_CONDA_ENV", "forecast_env")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    for key in ("LC_ALL", "TZ"):
        monkeypatch.delenv(key, raising=False)
    binding = {"installed_locale": {"LANG": "en_US.UTF-8"}, "environment": {}}
    runtimes = {}
    for kind, name in (("native", "forecast_env"), ("successor", "forecast_env_blackbox_v1")):
        prefix = tmp_path / name
        (prefix / "conda-meta").mkdir(parents=True)
        executable = prefix / "python"
        executable.write_bytes(b"captured interpreter")
        record = prefix / "conda-meta/python.json"
        record.write_bytes(b"captured package record")
        runtimes[name] = SimpleNamespace(executable=executable, prefix=prefix)
        binding["environment"][kind] = {
            "executable": str(executable), "prefix": str(prefix),
            "files": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (executable, record)},
            "effective_locale": {"LANG": "en_US.UTF-8", **({"TZ": "Asia/Shanghai"} if kind == "successor" else {})},
        }
    monkeypatch.setattr(runner, "_python_runtime", lambda profile: runtimes[profile.conda_env])
    migration._verify_reviewed_w3b_runtime(binding, tmp_path)
    record.write_bytes(b"changed package record")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        migration._verify_reviewed_w3b_runtime(binding, tmp_path)
    record.write_bytes(b"captured package record")
    (record.parent / "added.json").write_bytes(b"additional package")
    with pytest.raises(ValueError, match="inventory changed"):
        migration._verify_reviewed_w3b_runtime(binding, tmp_path)
