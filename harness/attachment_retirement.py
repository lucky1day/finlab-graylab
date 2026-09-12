"""临时 ECS 附件退役：仅复用同算法状态并切真实版本，不运行算法或回填历史。"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from harness.same_id_runtime_upgrade import (
    _assert_no_algorithm_process, _assert_no_temporary_writer,
    _capture_installed_locale, _read_candidate_states, _verified_install,
)
from harness.w3b_prepare import _ready_input
from scheduler import repository as repo
from scheduler.blackbox_state import _decode, _encode, _MAX_ENVELOPE_BYTES, read_regular_bytes
from scheduler.blackbox_v2_runner import _load_runtime_profile, _state_session, state_binding_for_scheme
from scheduler.discovery import load_scheme_config
from shared.blackbox_v2.environment_manifest import load_environment_fingerprint
from shared.blackbox_v2.snapshot import compose_blackbox_input_bundle
from shared.input_artifacts import open_blackbox_runtime_view
from shared.scheme_config_schema import NATIVE_ATTACHMENT_SCHEMES


ROOT = Path(__file__).resolve().parents[1]
IDS = sorted(NATIVE_ATTACHMENT_SCHEMES)
CURRENT = Path('/opt/bond-factor-lab/current')
CADENCES = ('daily', 'weekly')


def _temporary_alias_bases():
    mapping = json.loads((ROOT / 'deploy/native_to_blackbox_migration_v1.json').read_bytes())
    return sorted({row['new_base_scheme_id'].removesuffix('_bbv2')
        for wave in mapping['waves'] for row in wave['targets'] if row['old_base_scheme_id'] in IDS})


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fence():
    """只读 installed 控制面；调用者显式围栏，不在库内停止服务。"""
    units = {}
    for cadence in CADENCES:
        for suffix in ('timer', 'service'):
            name = f'bond-factor-lab-prediction-{cadence}.{suffix}'
            active = subprocess.run(
                ['systemctl', 'is-active', name], check=False, capture_output=True, text=True, timeout=10)
            if active.returncode != 3 or active.stdout.strip() != 'inactive':
                raise RuntimeError(f'{name} must be explicitly fenced and idle')
            unit = Path('/etc/systemd/system') / name
            units[name] = _hash(unit)
    backend = subprocess.run(['systemctl', 'is-active', 'bond-factor-lab-backend.service'],
        check=False, capture_output=True, text=True, timeout=10)
    if backend.returncode != 3 or backend.stdout.strip() != 'inactive':
        raise RuntimeError('ECS Backend must be fenced to exclude manual triggers during version changes')
    _assert_no_algorithm_process(sorted(set(IDS) | set(_temporary_alias_bases())))
    return units


def _capture(args, engine):
    if (os.environ.get('BFL_DEPLOYMENT_TARGET') != 'aliyun-gray'
            or args.project_root != ROOT or args.project_root == args.reference_project_root
            or args.project_root != args.project_root.resolve(strict=True)
            or args.reference_project_root != args.reference_project_root.resolve(strict=True)):
        raise ValueError('attachment retirement requires the actual ECS candidate and distinct immutable reference')
    current = CURRENT.resolve(strict=True)
    if current not in (args.project_root, args.reference_project_root):
        raise ValueError('current is neither approved source nor candidate')
    if args.action in ('cutover', 'rollback') and current != args.project_root:
        raise ValueError('version mutation requires current to point to the approved candidate')
    installs = {label: _verified_install(path) for label, path in (
        ('source', args.reference_project_root), ('candidate', args.project_root))}
    if (installs['source']['archive_sha256'] != args.expected_source_archive_sha256
            or installs['candidate']['archive_sha256'] != args.expected_archive_sha256):
        raise ValueError('installed archive differs from explicitly approved SHA')
    units = _fence()
    locale = _capture_installed_locale(args.project_root, installed_current_root=current)
    temporary = _assert_no_temporary_writer(engine, _temporary_alias_bases())
    snapshot, inputs = _ready_input()
    fingerprint = load_environment_fingerprint(args.project_root, expected_runtime_profile='blackbox-v2-v1')
    old = {key: load_scheme_config(args.reference_project_root / 'schemes' / key / 'config.yaml') for key in IDS}
    new = {key: replace(load_scheme_config(args.project_root / 'schemes' / key / 'config.yaml'),
                        environment_fingerprint=fingerprint) for key in IDS}
    with engine.connect() as conn:
        for key in IDS:
            row = repo._read_scheme_version_conn(conn, old[key], for_update=False)
            if row is None or row['environment_fingerprint'] != fingerprint:
                raise RuntimeError('source exact is absent or runtime environment changed')
            old[key] = replace(old[key], environment_fingerprint=fingerprint, data_snapshot_id=row['data_snapshot_id'])
            new[key] = replace(new[key], data_snapshot_id=row['data_snapshot_id'])
    incremental = {key: cfg for key, cfg in old.items() if cfg.incremental_state}
    old_states = _read_candidate_states(args.project_root, incremental, snapshot)
    candidate_incremental = {key: new[key] for key in incremental
        if (Path(os.environ['BFL_RUNTIME_ROOT']) / 'blackbox-state' / key / f'{new[key].scheme_version}.state').exists()}
    new_states = _read_candidate_states(args.project_root, candidate_incremental, snapshot)
    operation = args.action if args.action in ('cutover', 'rollback') else args.operation
    for key in candidate_incremental if operation != 'rollback' else ():
        parent = Path(os.environ['BFL_RUNTIME_ROOT']) / 'blackbox-state' / key
        old_header, old_payload = _decode(read_regular_bytes(parent / f'{old[key].scheme_version}.state', _MAX_ENVELOPE_BYTES))
        new_raw = read_regular_bytes(parent / f'{new[key].scheme_version}.state', _MAX_ENVELOPE_BYTES)
        expected = old_header | {'identity': old_header['identity'] | {'scheme_version': new[key].scheme_version}}
        if new_raw != _encode(expected, old_payload):
            raise RuntimeError('candidate state must change only the exact-version envelope')
    control = {'schema_version': 'blackbox-attachment-retirement-control-v1', 'scheme_ids': IDS,
        'writers_fenced': True, 'algorithm_executions': 0, 'state_ready': set(new_states) == set(incremental),
        'current_release': str(current), 'installs': installs, 'units': units, 'locale': locale,
        'temporary_writers': temporary, 'input': inputs, 'environment_fingerprint': fingerprint,
        'source_states': old_states, 'candidate_states': new_states,
        'tool_sha256': _hash(Path(__file__))}
    return old, new, snapshot, control


def _prepare_states(args, old, new, snapshot):
    """使用原状态发布器，只改版本封装；旧 input 与数组绝不伪装成当前输入的计算。"""
    profile = _load_runtime_profile(ROOT / 'deploy/blackbox_v2/runtime_profile_v1.json')
    converted = {}
    with open_blackbox_runtime_view(compose_blackbox_input_bundle(snapshot, factor_input_mode='algorithm_managed')) as view:
        for key in IDS:
            if not new[key].incremental_state:
                continue
            binding = state_binding_for_scheme(new[key], generation_id=snapshot.generation_id, persistent=True, rebuild=True)
            with tempfile.TemporaryDirectory(prefix='bfl-attachment-state-') as work:
                with _state_session(binding, Path(work), new[key].blackbox_metadata, new[key].delivery_script,
                                    view.data_dir, view.bundle.combined_snapshot_id, profile) as session:
                    parent = binding.root / key
                    source_path = parent / f'{old[key].scheme_version}.state'
                    source = read_regular_bytes(source_path, _MAX_ENVELOPE_BYTES)
                    header, payload = _decode(source)
                    if header['identity'] != session.identity | {'scheme_version': old[key].scheme_version}:
                        raise RuntimeError('source state code/metadata/environment identity is not reusable')
                    expected = _encode(header | {'identity': session.identity}, payload)
                    destination = parent / f'{new[key].scheme_version}.state'
                    if os.path.lexists(destination):
                        if read_regular_bytes(destination, _MAX_ENVELOPE_BYTES) != expected:
                            raise RuntimeError('existing candidate state differs; never overwrite')
                    else:
                        session.input_identity = dict(header['input'])
                        session.output_path.parent.mkdir()
                        session.output_path.write_bytes(payload)
                        session.publish()
                    if (read_regular_bytes(source_path, _MAX_ENVELOPE_BYTES) != source
                            or read_regular_bytes(destination, _MAX_ENVELOPE_BYTES) != expected):
                        raise RuntimeError('state envelope conversion read-back differs')
                    converted[key] = {'source_sha256': hashlib.sha256(source).hexdigest(),
                        'candidate_sha256': hashlib.sha256(expected).hexdigest(),
                        'payload_sha256': header['payload_sha256'], 'input_preserved': True}
    return converted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'prepare-states', 'cutover', 'rollback'))
    parser.add_argument('--operation', choices=('cutover', 'rollback'), default='cutover')
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--reference-project-root', type=Path, required=True)
    parser.add_argument('--expected-archive-sha256', required=True)
    parser.add_argument('--expected-source-archive-sha256', required=True)
    parser.add_argument('--expected-database-name', required=True)
    parser.add_argument('--expected-server-uuid', required=True)
    parser.add_argument('--expected-plan-sha256')
    parser.add_argument('--approved-by')
    args = parser.parse_args(argv)
    if args.action != 'preflight' and (not args.expected_plan_sha256 or not (args.approved_by or '').strip()):
        parser.error('mutation requires the fresh plan SHA and explicit operator')
    engine = repo.create_engine_from_env()
    try:
        old, new, snapshot, control = _capture(args, engine)
        operation = args.action if args.action in ('cutover', 'rollback') else args.operation
        kwargs = dict(old_configs=old, new_configs=new, action=operation,
            expected_database_name=args.expected_database_name, expected_server_uuid=args.expected_server_uuid)
        plan = repo.read_blackbox_attachment_retirement_plan(engine, **kwargs, control_plane_evidence=control)
        sha = repo.native_successor_plan_sha256(plan)
        if args.action == 'preflight':
            result = {'plan': plan, 'plan_sha256': sha}
        elif args.action == 'prepare-states':
            if operation != 'cutover' or sha != args.expected_plan_sha256:
                raise RuntimeError('state preparation plan changed or is not cutover')
            result = {'plan_sha256': sha, 'states': _prepare_states(args, old, new, snapshot),
                'algorithm_executions': 0, 'database_writes': 0, 'approved_by': args.approved_by}
            _capture(args, engine)
        else:
            result = repo.apply_blackbox_attachment_retirement(engine, **kwargs,
                expected_plan_sha256=args.expected_plan_sha256, approved_by=args.approved_by,
                approved_at=datetime.now(timezone.utc), control_plane_evidence_reader=lambda: _capture(args, engine)[3])
        print(json.dumps(result, sort_keys=True, default=str))
    finally:
        engine.dispose()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
