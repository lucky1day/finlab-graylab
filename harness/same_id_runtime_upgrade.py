"""仅服务 ECS W3B 的临时同 ID 控制层；不计算算法、不发布状态或切换 release。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
from typing import Mapping, Sequence

from sqlalchemy import text

from harness.native_successor_migration import (
    _CURRENT_LINKS,
    _DATABRIDGE_FILES,
    _REVIEWED_W3B_EVIDENCE,
    _capture_scheduler_state,
    _json_sha256,
    _sha256_file,
    _source_tree_sha256,
    load_native_successor_waves,
)
from harness.runtime_upgrade_evidence import verify_reviewed_w3b_delivery_change
from scheduler.discovery import load_scheme_config
from scheduler.repository import (
    apply_same_id_runtime_upgrade,
    native_successor_plan_sha256,
    read_same_id_runtime_upgrade_plan,
)
from shared.blackbox_v2.environment_manifest import load_environment_fingerprint
from shared.input_artifacts import get_ready_blackbox_snapshot, open_blackbox_runtime_view
from shared.blackbox_v2.snapshot import compose_blackbox_input_bundle


W3B_IDS = tuple(sorted(_REVIEWED_W3B_EVIDENCE))
_EXECUTION_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SERVICE_ENV_FILE = Path("/etc/bond-factor-lab/bond-factor-lab.env")
_ECS_BLACKBOX_PREFIX = Path("/opt/miniconda3/envs/forecast_env_blackbox_v1")


def parse_w3b_harness_run_ids(values: Sequence[str]) -> dict[str, str]:
    """只接受三个固定原 ID 的不同真实 run ID，不接受 JSON 成功声明。"""
    parsed = {}
    for value in values:
        scheme_id, separator, run_id = value.partition("=")
        if not separator or scheme_id not in W3B_IDS or scheme_id in parsed:
            raise ValueError("W3B requires exactly one BASE=RUN for each original ID")
        if not run_id or run_id != run_id.strip() or len(run_id) > 128:
            raise ValueError("invalid W3B Harness run ID")
        parsed[scheme_id] = run_id
    if set(parsed) != set(W3B_IDS) or len(set(parsed.values())) != 3:
        raise ValueError("W3B requires the complete three-scheme Harness run set")
    return parsed


def _verified_install(root: Path) -> dict[str, object]:
    """校验实际 releases 目录内的只读安装树和安装记录。"""
    root = root.resolve(strict=True)
    record_path = root / ".bfl-release-install.json"
    record = json.loads(record_path.read_bytes())
    fields = {"schema_version", "commit", "archive_sha256", "source_tree_sha256", "runtime_root"}
    if not isinstance(record, dict) or set(record) != fields:
        raise RuntimeError("invalid immutable release install record")
    if record["schema_version"] != "bfl-source-release-install-v1":
        raise RuntimeError("unsupported immutable release install record")
    commit = record["commit"]
    if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise RuntimeError("invalid immutable release commit")
    for key in ("archive_sha256", "source_tree_sha256"):
        value = record[key]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise RuntimeError("invalid immutable release digest")
    if root != _CURRENT_LINKS["aliyun-gray"].parent / "releases" / commit:
        raise RuntimeError("migration requires an installed immutable release path")
    if record["runtime_root"] != os.environ.get("BFL_RUNTIME_ROOT"):
        raise RuntimeError("release runtime root differs from execution environment")
    runtime_root = Path(record["runtime_root"])
    native_cache = runtime_root / "cache" / "native" / commit
    expected_environment = (
        f"BFL_RELEASE_COMMIT={commit}\n"
        f"BFL_RUNTIME_ROOT={json.dumps(str(runtime_root))}\n"
        f"NUMBA_CACHE_DIR={json.dumps(str(native_cache / 'numba'))}\n"
        f"MPLCONFIGDIR={json.dumps(str(native_cache / 'matplotlib'))}\n"
    )
    if (root / ".bfl-release.env").read_text() != expected_environment:
        raise RuntimeError("immutable release environment file changed")
    for path in (root, *root.rglob("*")):
        if path.is_symlink() or stat.S_IMODE(path.lstat().st_mode) & 0o222:
            raise RuntimeError("migration requires a read-only symlink-free release tree")
    if _source_tree_sha256(root) != record["source_tree_sha256"]:
        raise RuntimeError("immutable release source tree changed")
    return record | {"install_record_sha256": _sha256_file(record_path)}


def _verified_pair(project_root: Path, reference_project_root: Path):
    """同时校验原 Native、交付身份及 W3B 精确周频修订补丁。"""
    root, reference = project_root.resolve(strict=True), reference_project_root.resolve(strict=True)
    candidate_install, reference_install = _verified_install(root), _verified_install(reference)
    if root == reference:
        raise RuntimeError("candidate and Native reference releases must differ")
    wave = load_native_successor_waves(root / "deploy/native_to_blackbox_migration_v1.json")["W3B"]
    if tuple(sorted(target.old_base_scheme_id for target in wave.targets)) != W3B_IDS or not wave.atomic_family:
        raise RuntimeError("W3B fixed atomic mapping changed")
    matrix_path = root / "deploy/scheme_deployment_matrix_v1.json"
    matrix = json.loads(matrix_path.read_bytes())
    schemes = matrix.get("schemes") if isinstance(matrix, dict) else None
    if not isinstance(schemes, dict) or matrix.get("schema_version") != "scheme-deployment-matrix-v1":
        raise RuntimeError("invalid same-ID deployment matrix")
    old_configs, new_configs, conversions = {}, {}, {}
    for scheme_id in W3B_IDS:
        alias = scheme_id + "_bbv2"
        if "aliyun-gray" not in schemes.get(scheme_id, []) or "aliyun-gray" in schemes.get(alias, []):
            raise RuntimeError("W3B matrix must deploy original IDs and exclude temporary bbv2 IDs")
        old = load_scheme_config(reference / "schemes" / scheme_id / "config.yaml")
        source = load_scheme_config(reference / "schemes" / alias / "config.yaml")
        new = load_scheme_config(root / "schemes" / scheme_id / "config.yaml")
        approved = _REVIEWED_W3B_EVIDENCE[scheme_id]
        if old.runtime_type != "native_adapter" or old.code_hash != approved["old_code_hash"]:
            raise RuntimeError("reference Native differs from reviewed W3B algorithm")
        if source.runtime_type != "blackbox_v2" or new.runtime_type != "blackbox_v2" or not new.incremental_state or new.factor_input_mode != "algorithm_managed":
            raise RuntimeError("W3B requires genuine incremental Blackbox deliveries")
        if source.code_hash != approved["code_sha256"] or source.manifest_hash != approved["metadata_sha256"]:
            raise RuntimeError("reference Blackbox differs from reviewed W3B delivery")
        conversions[scheme_id] = verify_reviewed_w3b_delivery_change(
            project_root=root, source_script=source.delivery_script.read_bytes(),
            source_metadata=source.delivery_metadata.read_bytes(),
            candidate_script=new.delivery_script.read_bytes(),
            candidate_metadata=new.delivery_metadata.read_bytes(),
        )
        old_configs[scheme_id], new_configs[scheme_id] = old, new
    return old_configs, new_configs, {
        "candidate": candidate_install, "reference": reference_install,
        "identity_conversions": conversions, "deployment_matrix_sha256": _sha256_file(matrix_path),
    }


def _assert_no_temporary_writer(engine, scheme_ids=None) -> dict[str, object]:
    """只读拒绝已退役设计中的临时 bbv2 Writer 和在途运行。"""
    params = {f"id_{index}": scheme_id + "_bbv2" for index, scheme_id in enumerate(W3B_IDS if scheme_ids is None else scheme_ids)}
    ids = ",".join(f":{key}" for key in params)
    checks = (
        ("t_scheme_registry", "base_scheme_id", "status = 'active'"),
        ("t_scheme_versions", "scheme_id", "status = 'active'"),
        ("t_scheme_runs", "scheme_id", "status = 'running'"),
        ("t_backtest_runs", "scheme_id", "status = 'running'"),
        ("t_harness_runs", "scheme_id", "status = 'running'"),
    )
    with engine.connect() as conn:
        for table, column, condition in checks:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({ids}) AND {condition}"), params).scalar_one()
            if count:
                raise RuntimeError(f"temporary W3B bbv2 second Writer/in-flight row: {table}")
    return {"scheme_ids": sorted(params.values()), "temporary_active_or_running_count": 0}


def _assert_no_algorithm_process(scheme_ids=None) -> None:
    """补充 cadence 围栏，拒绝绕过调度入口直接启动的 W3B CLI。"""
    names = "|".join(scheme_id + "(_bbv2)?" for scheme_id in (W3B_IDS if scheme_ids is None else scheme_ids))
    pattern = (rf"(^|/)({names})\.py([[:space:]]|$)|schemes\.({names})\.predict"
               rf"|scheduler\.scheme_runner.*--scheme-id[ =]+({names})([[:space:]]|$)")
    result = subprocess.run(["/usr/bin/pgrep", "-f", pattern], check=False, capture_output=True, timeout=10)
    if result.returncode != 1:
        raise RuntimeError("W3B algorithm process exists or process inspection failed")
    if scheme_ids is not None:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                arguments = (entry / "cmdline").read_bytes().split(b"\0")
            except FileNotFoundError:
                continue
            if arguments and b"python" in Path(os.fsdecode(arguments[0])).name.encode():
                if any(key.encode() in value for key in scheme_ids for value in arguments):
                    raise RuntimeError("single-Request matching Python process is still running")


def _assert_execution_modules(root: Path) -> None:
    """禁止工作树控制层借用已安装候选的 manifest 执行未发布代码。"""
    from scheduler import repository, blackbox_v2_runner, blackbox_state
    from harness import runtime_upgrade_evidence, native_successor_migration, runtime_upgrade_state, w3b_state_source, w3b_revision_evidence
    for module in (repository, blackbox_v2_runner, blackbox_state,
                   runtime_upgrade_evidence, native_successor_migration,
                   runtime_upgrade_state, w3b_state_source, w3b_revision_evidence):
        if root not in Path(module.__file__).resolve(strict=True).parents:
            raise RuntimeError("migration modules must all originate in the candidate release")


def _capture_installed_locale(root: Path, *, installed_current_root: Path | None = None) -> dict[str, object]:
    """复用已核验 W3B 初始化的封闭 systemd 环境读法，不改调用进程环境。"""
    from scheduler.blackbox_v2_runner import _load_runtime_profile, _python_runtime
    unit = subprocess.check_output([
        "/usr/bin/systemctl", "show", "bond-factor-lab-prediction-daily.service",
        "-p", "Environment", "-p", "EnvironmentFiles", "-p", "PassEnvironment", "-p", "UnsetEnvironment",
    ], text=True, timeout=10)
    properties = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line)
    # systemctl 按文件重复输出 EnvironmentFiles=，不能让 dict 丢掉前面的文件。
    properties["EnvironmentFiles"] = " ".join(
        line.partition("=")[2] for line in unit.splitlines() if line.startswith("EnvironmentFiles=")
    )
    expected_files = "/etc/bond-factor-lab/bond-factor-lab.env (ignore_errors=no) /opt/bond-factor-lab/current/.bfl-release.env (ignore_errors=no)"
    if properties.get("EnvironmentFiles") != expected_files or properties.get("PassEnvironment") != "" or properties.get("UnsetEnvironment") != "":
        raise RuntimeError("unsupported installed daily environment configuration")
    manager = subprocess.check_output(["/usr/bin/systemctl", "show-environment"], text=True, timeout=10)
    contents = [manager, "\n".join(shlex.split(properties.get("Environment", ""))),
                _SERVICE_ENV_FILE.read_text(),
                ((installed_current_root or root) / ".bfl-release.env").read_text()]
    selected = {}
    for content in contents:
        for line in content.splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() in {"LANG", "LC_ALL", "TZ", "PATH"}:
                tokens = shlex.split(value)
                if len(tokens) > 1:
                    raise RuntimeError("ambiguous installed service locale")
                selected[key.strip()] = tokens[0] if tokens else ""
    locale = {key: value for key, value in selected.items() if key != "PATH"}
    # 本阶段只接受已受控初始化/复验过的 ECS locale，不扩展任意 systemd 配置解析。
    if locale != {"LANG": "en_US.UTF-8"}:
        raise RuntimeError("installed locale differs from reviewed W3B ECS environment")
    profile = _load_runtime_profile(root / "deploy/blackbox_v2/runtime_profile_v1.json")
    installed_conda = shutil.which("conda", path=selected.get("PATH", ""))
    caller_conda = shutil.which("conda")
    if not installed_conda or not caller_conda or Path(installed_conda).resolve(strict=True) != Path(caller_conda).resolve(strict=True):
        raise RuntimeError("migration shell conda differs from installed daily service conda")
    runtime = _python_runtime(profile)
    if runtime.prefix.resolve(strict=True) != _ECS_BLACKBOX_PREFIX.resolve(strict=True):
        raise RuntimeError("W3B Blackbox runtime prefix differs from reviewed ECS runtime")
    effective = dict(profile.environment_defaults) | locale
    caller = dict(profile.environment_defaults) | {key: os.environ[key] for key in profile.environment_allowlist if key in os.environ}
    if caller != effective:
        raise RuntimeError("migration shell locale differs from installed daily service locale")
    return {"installed_environment_sha256": _json_sha256(unit), "effective_locale": effective,
            "conda_executable": str(Path(installed_conda).resolve(strict=True)), "python_executable": str(runtime.executable)}


def _read_candidate_states(root: Path, new_configs, snapshot) -> dict[str, object]:
    """用实际 StateSession 验证 canonical 封装；只写私有临时输入，不 publish。"""
    from scheduler.blackbox_v2_runner import _load_runtime_profile, _state_session, state_binding_for_scheme
    from scheduler.blackbox_state import read_regular_bytes, MAX_STATE_BYTES, _MAX_ENVELOPE_BYTES

    profile = _load_runtime_profile(root / "deploy/blackbox_v2/runtime_profile_v1.json")
    states = {}
    bundle = compose_blackbox_input_bundle(snapshot, factor_input_mode="algorithm_managed")
    with open_blackbox_runtime_view(bundle) as view:
        for scheme_id, cfg in new_configs.items():
            binding = state_binding_for_scheme(cfg, generation_id=snapshot.generation_id, persistent=True)
            if binding is None or binding.root is None:
                raise RuntimeError("W3B candidate requires persistent exact-version state")
            destination = binding.root / scheme_id / f"{cfg.scheme_version}.state"
            if not destination.is_file() or not (destination.parent / "state.lock").is_file():
                raise RuntimeError("W3B canonical state is absent; preflight does not initialize state")
            before = hashlib.sha256(read_regular_bytes(destination, _MAX_ENVELOPE_BYTES)).hexdigest()
            with tempfile.TemporaryDirectory(prefix="bfl-w3b-state-inspect-") as temporary:
                with _state_session(binding, Path(temporary), cfg.blackbox_metadata, cfg.delivery_script,
                                    view.data_dir, view.bundle.combined_snapshot_id, profile) as session:
                    if session.input_path is None:
                        raise RuntimeError("W3B canonical state has no reusable input")
                    states[scheme_id] = {
                        "envelope_sha256": before,
                        "payload_sha256": hashlib.sha256(read_regular_bytes(session.input_path, MAX_STATE_BYTES)).hexdigest(),
                        "identity_sha256": _json_sha256(session.identity),
                    }
            if hashlib.sha256(read_regular_bytes(destination, _MAX_ENVELOPE_BYTES)).hexdigest() != before:
                raise RuntimeError("W3B canonical state changed during read-only inspection")
    return states


def _capture(engine, project_root: Path, reference_project_root: Path):
    """每次从实际安装树、ready 输入、控制面和状态重新采集，不接受用户 JSON。"""
    if os.environ.get("BFL_DEPLOYMENT_TARGET") != "aliyun-gray":
        raise RuntimeError("same-ID migration currently supports ECS W3B only")
    root = project_root.resolve(strict=True)
    if root != _EXECUTION_PROJECT_ROOT:
        raise RuntimeError("migration control code must execute from the candidate immutable release")
    _assert_execution_modules(root)
    old, new, releases = _verified_pair(root, reference_project_root)
    current = _CURRENT_LINKS["aliyun-gray"]
    if not current.is_symlink() or current.resolve(strict=True) != root:
        raise RuntimeError("same-ID migration must execute from the fenced candidate current release")
    if os.environ.get("BFL_RELEASE_COMMIT") != releases["candidate"]["commit"]:
        raise RuntimeError("candidate current commit differs from execution environment")
    scheduler = _capture_scheduler_state(project_root=root, deployment_target="aliyun-gray", cadence="daily")
    locale = _capture_installed_locale(root)
    _assert_no_algorithm_process()
    temporary = _assert_no_temporary_writer(engine)
    snapshot = get_ready_blackbox_snapshot(snapshot_date=date.today().isoformat(), require_fresh=False, factor_input_mode="algorithm_managed")
    manifest = json.loads(snapshot.manifest_path.read_bytes())
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or set(files) != _DATABRIDGE_FILES:
        raise RuntimeError("W3B requires the ready five-file DataBridge generation")
    hashes = {}
    for filename in sorted(_DATABRIDGE_FILES):
        digest = _sha256_file(snapshot.data_dir / filename)
        if not isinstance(files[filename], dict) or files[filename].get("sha256") != digest:
            raise RuntimeError("ready DataBridge file differs from its manifest")
        hashes[filename] = digest
    fingerprint = load_environment_fingerprint(root, expected_runtime_profile="blackbox-v2-v1")
    new = {key: replace(cfg, environment_fingerprint=fingerprint, data_snapshot_id=snapshot.snapshot_id) for key, cfg in new.items()}
    return old, new, {
        "schema_version": "same-id-w3b-control-plane-v1", "wave": "W3B", "deployment_target": "aliyun-gray",
        "release": releases, "current_release": str(root), "scheduler": scheduler,
        "temporary_writer_check": temporary, "blackbox_environment_fingerprint": fingerprint,
        "execution_environment": locale,
        "native_canonical_selection": {
            key: {field: getattr(cfg, field) for field in (
                "scheme_version", "runtime_type", "code_hash", "config_hash", "manifest_hash",
            )} for key, cfg in old.items()
        },
        "databridge": {"generation_id": snapshot.generation_id, "data_snapshot_id": snapshot.snapshot_id,
                       "business_digest": snapshot.business_digest, "files": hashes},
        "candidate_states": _read_candidate_states(root, new, snapshot),
    }


def build_same_id_preflight(engine, *, project_root: Path, reference_project_root: Path,
                            wave: str, harness_run_ids: Mapping[str, str], action: str,
                            expected_database_name: str, expected_server_uuid: str) -> dict[str, object]:
    """生成可人工批准的完整 W3B 同 ID 计划；缺 DB Gate 证据直接阻断。"""
    if wave != "W3B":
        raise ValueError("only the complete W3B same-ID wave is implemented")
    run_ids = parse_w3b_harness_run_ids([f"{key}={value}" for key, value in harness_run_ids.items()])
    old, new, control = _capture(engine, project_root, reference_project_root)
    plan = read_same_id_runtime_upgrade_plan(engine, old_configs=old, new_configs=new, harness_run_ids=run_ids,
        action=action, control_plane_evidence=control, expected_database_name=expected_database_name,
        expected_server_uuid=expected_server_uuid)
    return {"plan": plan, "plan_sha256": native_successor_plan_sha256(plan)}


def capture_single_request(engine, *, project_root: Path, reference_project_root: Path,
                           wave: str, scheme_ids, preparing: bool, rollback: bool = False):
    """五个固定原身份的真实 ECS 只读就绪；不把 Mac 回归当作 ECS 执行。"""
    from scheduler.repository import _SAME_ID_SINGLE_REQUEST_WAVES
    from scheduler.blackbox_state import read_regular_bytes
    from harness.single_request_prepare import load_acceptance, load_ecs_seed

    ids = sorted(scheme_ids)
    if wave not in _SAME_ID_SINGLE_REQUEST_WAVES or not ids or len(ids) != len(set(ids)) or not set(ids) <= _SAME_ID_SINGLE_REQUEST_WAVES[wave]:
        raise ValueError("single-Request migration requires selected original IDs in its fixed wave")
    root, reference = project_root.resolve(strict=True), reference_project_root.resolve(strict=True)
    if root != _EXECUTION_PROJECT_ROOT or os.environ.get("BFL_DEPLOYMENT_TARGET") != "aliyun-gray":
        raise RuntimeError("single-Request control must execute in its immutable ECS candidate")
    _assert_execution_modules(root)
    from harness import single_request_prepare
    if root not in Path(single_request_prepare.__file__).resolve().parents:
        raise RuntimeError("single-Request preparation module outside candidate")
    releases = {"candidate": _verified_install(root), "reference": _verified_install(reference)}
    if root == reference or os.environ.get("BFL_RELEASE_COMMIT") != releases["candidate"]["commit"]:
        raise RuntimeError("single-Request candidate/reference process identity differs")
    current = _CURRENT_LINKS["aliyun-gray"]
    expected_current = reference if preparing else root
    if not current.is_symlink() or current.resolve(strict=True) != expected_current:
        raise RuntimeError("single-Request current differs from preparation/cutover phase")
    if not preparing and (current.parent / "previous").resolve(strict=True) != reference:
        raise RuntimeError("single-Request rollback release differs from actual previous")
    declared = load_native_successor_waves(root / "deploy/native_to_blackbox_migration_v1.json")[wave]
    if {target.old_base_scheme_id for target in declared.targets} != _SAME_ID_SINGLE_REQUEST_WAVES[wave]:
        raise RuntimeError("single-Request fixed wave mapping changed")
    scheduler = _capture_scheduler_state(project_root=expected_current, deployment_target="aliyun-gray", cadence="daily")
    locale = _capture_installed_locale(root, installed_current_root=expected_current)
    _assert_no_algorithm_process(ids)
    temporary = _assert_no_temporary_writer(engine, ids)
    matrix_path = root / "deploy/scheme_deployment_matrix_v1.json"
    matrix = json.loads(read_regular_bytes(matrix_path, 1024**2))["schemes"]
    old, new = {}, {}
    for key in ids:
        if "aliyun-gray" not in matrix.get(key, []) or "aliyun-gray" in matrix.get(key + "_bbv2", []):
            raise RuntimeError("single-Request matrix allows a temporary Writer")
        old[key] = load_scheme_config(reference / "schemes" / key / "config.yaml")
        new[key] = load_scheme_config(root / "schemes" / key / "config.yaml")
        if old[key].runtime_type != "native_adapter" or new[key].runtime_type != "blackbox_v2":
            raise RuntimeError("single-Request migration must retain original Native business identity")
    snapshot = get_ready_blackbox_snapshot(snapshot_date=date.today().isoformat(), require_fresh=False, factor_input_mode="algorithm_managed")
    manifest = json.loads(read_regular_bytes(snapshot.manifest_path, 1024**2))
    if set(manifest.get("files", {})) != _DATABRIDGE_FILES:
        raise RuntimeError("single-Request requires ready five-file input")
    hashes = {name: _sha256_file(snapshot.data_dir / name) for name in sorted(_DATABRIDGE_FILES)}
    if hashes != {name: item["sha256"] for name, item in manifest["files"].items()}:
        raise RuntimeError("single-Request ready input bytes changed")
    fingerprint = load_environment_fingerprint(root, expected_runtime_profile="blackbox-v2-v1")
    new = {key: replace(cfg, environment_fingerprint=fingerprint, data_snapshot_id=snapshot.snapshot_id) for key, cfg in new.items()}
    acceptance = {key: load_acceptance(old[key], new[key]) for key in ids}
    stateful = {key: cfg for key, cfg in new.items() if cfg.incremental_state}
    seeds = {key: load_ecs_seed(cfg, snapshot, hashes, require_unpublished=not rollback)
             for key, cfg in stateful.items()}
    evidence = {"schema_version": "single-request-migration-control-v1", "wave": wave, "scheme_ids": ids,
        "deployment_target": "aliyun-gray", "release": releases, "scheduler": scheduler,
        "temporary_writer_check": temporary, "blackbox_environment_fingerprint": fingerprint,
        "execution_environment": locale, "native_canonical_selection": {
            key: {field: getattr(cfg, field) for field in ("scheme_version", "runtime_type", "code_hash", "config_hash", "manifest_hash")}
            for key, cfg in old.items()},
        "databridge": {"generation_id": snapshot.generation_id, "data_snapshot_id": snapshot.snapshot_id,
                       "business_digest": snapshot.business_digest, "files": hashes},
        "candidate_seed_readiness": seeds, "stateless_scheme_ids": sorted(set(ids) - set(stateful)),
        "acceptance": acceptance, "deployment_matrix_sha256": _sha256_file(matrix_path),
        "ecs_algorithm_executions": 0, "historical_equivalence": False}
    return old, new, evidence


def execute_same_id_upgrade(engine, *, project_root: Path, reference_project_root: Path,
                           wave: str, harness_run_ids: Mapping[str, str], action: str,
                           expected_plan_sha256: str, approved_by: str,
                           expected_database_name: str, expected_server_uuid: str) -> dict[str, object]:
    """只委托 repository 切换数据库；不启动算法、服务或更改 current。"""
    if wave != "W3B":
        raise ValueError("only the complete W3B same-ID wave is implemented")
    run_ids = parse_w3b_harness_run_ids([f"{key}={value}" for key, value in harness_run_ids.items()])
    old, new, _control = _capture(engine, project_root, reference_project_root)
    return apply_same_id_runtime_upgrade(engine, old_configs=old, new_configs=new, harness_run_ids=run_ids,
        action=action, expected_plan_sha256=expected_plan_sha256, approved_by=approved_by,
        approved_at=datetime.now(timezone.utc), expected_database_name=expected_database_name,
        expected_server_uuid=expected_server_uuid,
        additional_lifecycle_lock_scheme_ids=tuple(key + "_bbv2" for key in W3B_IDS),
        control_plane_evidence_reader=lambda: _capture(engine, project_root, reference_project_root)[2])
