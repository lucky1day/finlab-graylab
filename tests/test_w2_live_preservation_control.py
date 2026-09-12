"""临时 W2 历史物化控制边界；不连接现场、不运行算法。"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness import cli, w2_live_preservation as control


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def backup(tmp_path, monkeypatch):
    backup = {'schema_version': 'w2-scoped-recoverable-backup-v1',
              'scope': sorted((*control._IDS, *(key + '_bbv2' for key in control._IDS))),
              'source_database_identity_sha256': 'c' * 64,
              'source_rows_sha256': 'a' * 64, 'existing_facts_sha256': 'b' * 64}
    digest = _save(tmp_path / 'w2-scoped-backup.json', backup)
    receipt = {'status': 'passed', 'backup_sha256': digest, 'foreign_keys_enabled': True,
               'isolated_schema_removed': True, 'source_rows_sha256': 'a' * 64, 'existing_facts_sha256': 'b' * 64}
    restored = _save(tmp_path / 'w2-restore-receipt.json', receipt)
    monkeypatch.setattr(control, '_EVIDENCE', tmp_path)
    monkeypatch.setattr(control, '_BACKUP_SHA', digest)
    monkeypatch.setattr(control, '_RESTORE_SHA', restored)
    return tmp_path, receipt


def test_fixed_restored_backup_produces_repository_proof(backup):
    original, proof = control._backup()
    assert proof['restoration_verified'] is True
    assert proof['source_rows_sha256'] == original['source_rows_sha256']
    assert proof['restore_receipt_sha256'] == control._RESTORE_SHA


@pytest.mark.parametrize('failure', ['backup_bytes', 'receipt_bytes', 'constraints', 'restore_hash', 'history', 'scope', 'symlink'])
def test_bad_backup_cannot_authorize_materialization(backup, monkeypatch, failure):
    path, receipt = backup
    if failure == 'backup_bytes':
        (path / 'w2-scoped-backup.json').write_text('{}')
    elif failure == 'receipt_bytes':
        (path / 'w2-restore-receipt.json').write_text('{}')
    elif failure == 'symlink':
        original = path / 'w2-scoped-backup.json'
        original.rename(path / 'retained.json')
        original.symlink_to(path / 'retained.json')
    elif failure == 'scope':
        source = json.loads((path / 'w2-scoped-backup.json').read_bytes())
        source['scope'].append('unrelated')
        monkeypatch.setattr(control, '_BACKUP_SHA', _save(path / 'w2-scoped-backup.json', source))
    else:
        receipt[{'constraints': 'foreign_keys_enabled', 'restore_hash': 'backup_sha256', 'history': 'existing_facts_sha256'}[failure]] = False
        monkeypatch.setattr(control, '_RESTORE_SHA', _save(path / 'w2-restore-receipt.json', receipt))
    with pytest.raises((ValueError, OSError)):
        control._backup()


@pytest.fixture
def historical(tmp_path, monkeypatch):
    snapshot = 'snapshot-' + '1' * 24
    path = tmp_path / 'snapshots' / ('2' * 64) / snapshot / 'manifest.json'
    files = {}
    for name in control._FILES:
        payload = name.encode()
        target = path.parent / 'data' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        files[name] = {'sha256': hashlib.sha256(payload).hexdigest()}
    _save(path, {'data_snapshot_id': snapshot, 'files': files})
    _save(tmp_path / 'receipts' / ('2' * 64 + '.json'), {'identity': {'files': files, 'generation_id': 'old-generation'}})
    monkeypatch.setattr(control, '_CACHE_ROOT', tmp_path)
    monkeypatch.setattr(control, 'BLACKBOX_GENERATION_SNAPSHOT_ROOT', tmp_path)
    reader = MagicMock(return_value=SimpleNamespace(snapshot_id=snapshot, manifest_path=path, generation_id='old-generation'))
    monkeypatch.setattr(control, '_read_generation_snapshot_cache', reader)
    return {'sources': [{'run': {'data_snapshot_id': snapshot}}]}, path, reader


def test_materialization_reads_historical_generation_without_current_ready(historical):
    backup, path, reader = historical
    inputs, artifacts = control._historical_inputs(backup)
    proof, = inputs.values()
    assert proof['generation_id'] == 'old-generation'
    assert proof['files'].keys() == control._FILES
    assert list(artifacts.values())[0]['manifest_path'] == str(path)
    assert reader.call_args.kwargs == {'factor_input_mode': 'algorithm_managed'}


@pytest.mark.parametrize('failure', ['bytes', 'missing', 'duplicate', 'snapshot', 'root', 'receipt'])
def test_historical_input_mismatch_rejects(historical, monkeypatch, failure):
    backup, path, reader = historical
    if failure == 'bytes':
        (path.parent / 'data' / 'daily_output.csv').write_text('changed')
    elif failure == 'missing':
        path.unlink()
    elif failure == 'duplicate':
        second = path.parents[2] / ('3' * 64) / path.parent.name / 'manifest.json'
        _save(second, json.loads(path.read_bytes()))
    elif failure == 'snapshot':
        reader.return_value.snapshot_id = 'wrong'
    elif failure == 'root':
        monkeypatch.setattr(control, '_CACHE_ROOT', Path('/wrong'))
    else:
        reader.side_effect = ValueError('generation mismatch')
    with pytest.raises(ValueError):
        control._historical_inputs(backup)


def _args(action, wave='W2'):
    result = ['migrate-native-successor', action, '--wave', wave, '--project-root', '/candidate',
              '--reference-project-root', '/reference', '--expected-database-name', 'isolated', '--expected-server-uuid', 'test-only']
    return result + (['--action', 'preserve-live'] if action == 'preflight'
                     else ['--expected-plan-sha256', 'a' * 64, '--approved-by', 'tester'])


@pytest.mark.parametrize('action', ['preflight', 'preserve-live'])
def test_cli_uses_only_materialization_route(monkeypatch, action):
    engine, preflight, apply = MagicMock(), MagicMock(return_value={'readonly': True}), MagicMock(return_value={'written': 4})
    monkeypatch.setattr(cli, 'create_engine_from_env', lambda: engine)
    monkeypatch.setattr(cli, 'build_w2_live_preservation_preflight', preflight)
    monkeypatch.setattr(cli, 'execute_w2_live_preservation', apply)
    cli._run_native_successor_migration_command(cli._build_parser().parse_args(_args(action)))
    assert (preflight if action == 'preflight' else apply).call_count == 1
    (apply if action == 'preflight' else preflight).assert_not_called()
    engine.dispose.assert_called_once()


@pytest.mark.parametrize('arguments', [_args('preserve-live', 'W3A') + ['--rollback-project-root', '/irrelevant'], _args('preflight', 'W3B'),
    _args('preflight') + ['--harness-run-id', 'fake=run'], _args('preflight') + ['--predict-date', '2026-09-11']])
def test_unsupported_preservation_fails_before_database(monkeypatch, arguments):
    engine = MagicMock(side_effect=AssertionError('must not connect'))
    monkeypatch.setattr(cli, 'create_engine_from_env', engine)
    with pytest.raises(ValueError):
        cli._run_native_successor_migration_command(cli._build_parser().parse_args(arguments))
    engine.assert_not_called()


def test_apply_recaptures_control_inside_repository_transaction(monkeypatch):
    captures = MagicMock(return_value=({'source': 1}, {'target': 2}, {'backup': 3}, {'control': 4}))
    monkeypatch.setattr(control, '_capture', captures)
    def apply(_engine, **kwargs):
        assert captures.call_count == 1
        assert kwargs['control_plane_evidence_reader']() == {'control': 4}
        assert captures.call_count == 2
        return {'passed': True}
    monkeypatch.setattr(control.repository, 'apply_same_id_live_preservation', apply)
    assert control.execute_w2_live_preservation(None, project_root=Path('/candidate'), reference_project_root=Path('/reference'),
        expected_database_name='test', expected_server_uuid='test', expected_plan_sha256='a' * 64, approved_by='tester') == {'passed': True}
