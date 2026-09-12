"""临时 W2 四条历史 live 结果保全；不执行算法、不改旧事实、不清理身份。"""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from sqlalchemy import text

from harness import same_id_runtime_upgrade as control, writer_reclaim
from harness.runtime_upgrade_evidence import verify_identity_only_delivery_change
from scheduler import repository
from scheduler.blackbox_state import read_regular_bytes
from scheduler.discovery import load_scheme_config
from shared.input_artifacts import BLACKBOX_GENERATION_SNAPSHOT_ROOT, _read_generation_snapshot_cache


_ROOT = Path(__file__).resolve().parents[1]
_EVIDENCE = Path('/opt/bond-factor-lab/incoming/w2-writer-reclaim-ready-20260912.Rzepm0')
_BACKUP_SHA = '61148898d72c7b6f85de9b2653c70b9406b8ed3ce8302e9776ce1390ffe51b06'
_RESTORE_SHA = '93eaa9426fd0f34bb3f249fce780896b17f1564bdffed1c6fcc4d33a64f399b8'
_IDS = writer_reclaim.RECLAIM_WAVES['W2']
_FILES = {'daily_output.csv', 'weekly_output.csv', 'monthly_output.csv', 'api_wind_date.csv', 'factor_catalog.csv'}
_CACHE_ROOT = Path('/var/lib/bond-factor-lab/state/artifacts/blackbox_v2/snapshots/generation_cache')


def _backup():
    """只接纳已独立审查并真实恢复过的固定备份原件。"""
    raw = read_regular_bytes(_EVIDENCE / 'w2-scoped-backup.json', 16 * 1024**2)
    restored = read_regular_bytes(_EVIDENCE / 'w2-restore-receipt.json', 64 * 1024)
    if hashlib.sha256(raw).hexdigest() != _BACKUP_SHA or hashlib.sha256(restored).hexdigest() != _RESTORE_SHA:
        raise ValueError('W2 backup or restoration receipt changed')
    backup, receipt = json.loads(raw), json.loads(restored)
    if (backup['schema_version'] != 'w2-scoped-recoverable-backup-v1'
            or backup['scope'] != sorted((*_IDS, *(key + '_bbv2' for key in _IDS)))
            or receipt['status'] != 'passed' or receipt['backup_sha256'] != _BACKUP_SHA
            or receipt['foreign_keys_enabled'] is not True or receipt['isolated_schema_removed'] is not True
            or any(receipt[field] != backup[field] for field in ('source_rows_sha256', 'existing_facts_sha256'))):
        raise ValueError('W2 backup restore proof is not complete')
    proof = {'schema_version': 'same-id-live-preservation-backup-v1',
             'backup_uri': str(_EVIDENCE / 'w2-scoped-backup.json'), 'backup_sha256': _BACKUP_SHA,
             'restore_receipt_sha256': _RESTORE_SHA, 'restoration_verified': True,
             'source_database_identity_sha256': backup['source_database_identity_sha256'],
             'source_rows_sha256': backup['source_rows_sha256'], 'existing_facts_sha256': backup['existing_facts_sha256']}
    return backup, proof


def _historical_inputs(backup):
    """核对源 run 的真实历史 snapshot；不读取当前 ready 代替历史输入。"""
    root = BLACKBOX_GENERATION_SNAPSHOT_ROOT
    if root != _CACHE_ROOT:
        raise ValueError('W2 historical snapshot root differs')
    result, artifacts = {}, {}
    for snapshot_id in sorted({item['run']['data_snapshot_id'] for item in backup['sources']}):
        if not isinstance(snapshot_id, str) or not re.fullmatch(r'snapshot-[0-9a-f]{24}', snapshot_id):
            raise ValueError('invalid source snapshot ID')
        manifests = list((root / 'snapshots').glob('*/' + snapshot_id + '/manifest.json'))
        if len(manifests) != 1:
            raise ValueError('source snapshot absent or ambiguous')
        path = manifests[0]
        receipt_path = root / 'receipts' / (path.parent.parent.name + '.json')
        receipt_bytes = read_regular_bytes(receipt_path, 2 * 1024**2)
        receipt = json.loads(receipt_bytes)
        snapshot = _read_generation_snapshot_cache(root, receipt['identity'], factor_input_mode='algorithm_managed')
        if snapshot is None or snapshot.snapshot_id != snapshot_id or snapshot.manifest_path != path:
            raise ValueError('historical generation receipt differs from source snapshot')
        manifest_bytes = read_regular_bytes(path, 1024**2)
        manifest = json.loads(manifest_bytes)
        if manifest['data_snapshot_id'] != snapshot_id or set(manifest['files']) != _FILES:
            raise ValueError('historical manifest does not describe five-file source input')
        files = {name: hashlib.sha256(read_regular_bytes(path.parent / 'data' / name, 128 * 1024**2)).hexdigest()
                 for name in sorted(_FILES)}
        if (files != {name: value['sha256'] for name, value in manifest['files'].items()}
                or files != {name: value['sha256'] for name, value in receipt['identity']['files'].items()}):
            raise ValueError('historical snapshot bytes differ from manifest/generation')
        result[snapshot_id] = {'snapshot_id': snapshot_id, 'generation_id': snapshot.generation_id,
                              'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(), 'files': files}
        artifacts[snapshot_id] = {'manifest_path': str(path), 'generation_receipt_path': str(receipt_path),
                                  'generation_receipt_sha256': hashlib.sha256(receipt_bytes).hexdigest()}
    return result, artifacts


def _capture(engine, *, project_root: Path, reference_project_root: Path):
    """读取不可变 current、唯一 Writer 围栏、真实版本与已恢复备份。"""
    root, reference = project_root.resolve(strict=True), reference_project_root.resolve(strict=True)
    if (root != _ROOT or os.environ.get('BFL_DEPLOYMENT_TARGET') != 'aliyun-gray'
            or control._CURRENT_LINKS['aliyun-gray'].resolve(strict=True) != root
            or reference != Path('/opt/bond-factor-lab/releases/9385292514bdd2491c13bf7b24ba0a97e1d02e60')):
        raise ValueError('W2 preservation requires its immutable ECS current and takeover reference')
    control._assert_execution_modules(root)
    installs = {'candidate': control._verified_install(root), 'reference': control._verified_install(reference)}
    if os.environ.get('BFL_RELEASE_COMMIT') != installs['candidate']['commit']:
        raise ValueError('W2 preservation process commit differs')
    scheduler = control._capture_scheduler_state(project_root=root, deployment_target='aliyun-gray', cadence='daily')
    writer_reclaim._assert_no_algorithm_process('W2')
    sources, targets, conversions = {}, {}, {}
    matrix_path = root / 'deploy/scheme_deployment_matrix_v1.json'
    matrix = json.loads(read_regular_bytes(matrix_path, 1024**2))
    with engine.connect() as conn:
        for key in _IDS:
            if 'aliyun-gray' not in matrix['schemes'][key] or matrix['schemes'][key + '_bbv2'] != []:
                raise ValueError('W2 preservation matrix allows a second Writer')
            for collection, scheme_id in ((sources, key + '_bbv2'), (targets, key)):
                cfg = load_scheme_config(root / 'schemes' / scheme_id / 'config.yaml')
                previous = load_scheme_config(reference / 'schemes' / scheme_id / 'config.yaml')
                if repository._same_id_reclaim_identity(cfg) != repository._same_id_reclaim_identity(previous):
                    raise ValueError('W2 preservation changes an already accepted execution version')
                rows = conn.execute(text('SELECT environment_fingerprint,data_snapshot_id FROM t_scheme_versions '
                    'WHERE scheme_id=:id AND scheme_version=:version'), {'id': scheme_id, 'version': cfg.scheme_version}).mappings().all()
                if len(rows) != 1:
                    raise ValueError('W2 accepted exact version missing or ambiguous')
                collection[key] = replace(cfg, **dict(rows[0]))
            if not repository.read_blackbox_execution_approval(engine, targets[key]).executable:
                raise ValueError('W2 original ID is not executable')
            if repository.read_blackbox_execution_approval(engine, sources[key]).executable:
                raise ValueError('W2 temporary identity still executable')
            conversions[key] = verify_identity_only_delivery_change(project_root=root,
                source_script=sources[key].delivery_script.read_bytes(), source_metadata=sources[key].delivery_metadata.read_bytes(),
                candidate_script=targets[key].delivery_script.read_bytes(), candidate_metadata=targets[key].delivery_metadata.read_bytes())
    backup, proof = _backup()
    inputs, artifacts = _historical_inputs(backup)
    return sources, targets, proof, {'schema_version': 'same-id-live-preservation-control-plane-v1',
        'wave': 'W2', 'deployment_target': 'aliyun-gray', 'scheduler': scheduler, 'release': installs,
        'deployment_matrix_sha256': control._sha256_file(matrix_path),
        'source_canonical_selection': {key: repository._same_id_reclaim_identity(cfg) for key, cfg in sources.items()},
        'target_canonical_selection': {key: repository._same_id_reclaim_identity(cfg) for key, cfg in targets.items()},
        'identity_conversions': conversions, 'source_inputs': inputs, 'source_input_artifacts': artifacts}


def build_w2_live_preservation_preflight(engine, *, project_root: Path, reference_project_root: Path,
                                        expected_database_name: str, expected_server_uuid: str) -> dict:
    """只读输出四条物化计划和批准 SHA。"""
    sources, targets, backup, evidence = _capture(engine, project_root=project_root, reference_project_root=reference_project_root)
    plan = repository.read_same_id_live_preservation_plan(engine, source_configs=sources, target_configs=targets,
        backup_evidence=backup, control_plane_evidence=evidence,
        expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid)
    return {'plan': plan, 'plan_sha256': repository.native_successor_plan_sha256(plan)}


def execute_w2_live_preservation(engine, *, project_root: Path, reference_project_root: Path,
                                expected_database_name: str, expected_server_uuid: str,
                                expected_plan_sha256: str, approved_by: str) -> dict:
    """仅委托现有 repository 事务执行；文件漂移由锁内重读使批准失效。"""
    kwargs = dict(project_root=project_root, reference_project_root=reference_project_root)
    sources, targets, backup, _ = _capture(engine, **kwargs)
    return repository.apply_same_id_live_preservation(engine, source_configs=sources, target_configs=targets,
        backup_evidence=backup, control_plane_evidence_reader=lambda: _capture(engine, **kwargs)[3],
        expected_database_name=expected_database_name, expected_server_uuid=expected_server_uuid,
        expected_plan_sha256=expected_plan_sha256, approved_by=approved_by, approved_at=datetime.now(timezone.utc))
