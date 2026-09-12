"""W3A 临时保全凭证、历史输入与既有 CLI 边界；不运行真实算法。"""
import hashlib
import json
from unittest.mock import MagicMock

import pytest

from harness import cli, w2_live_preservation as control
from test_w2_live_preservation_control import _save, _args, historical


@pytest.fixture
def restored(tmp_path,monkeypatch):
    ids=control._ids('W3A')
    backup={'schema_version':'w3a-scoped-recoverable-backup-v1',
            'scope':sorted((*ids,*(key+'_bbv2' for key in ids))),
            'source_database_identity_sha256':'a'*64,'source_rows_sha256':'b'*64,
            'existing_facts_sha256':'c'*64,'records_sha256':'d'*64}
    backup_sha=_save(tmp_path/'w3a-scoped-backup.json',backup)
    receipt={'status':'passed','backup_sha256':backup_sha,'foreign_keys_enabled':True,'isolated_schema_removed':True,
             'source_rows_sha256':'b'*64,'existing_facts_sha256':'c'*64,'records_sha256':'d'*64,
             'source_xor_actuals_verified':True,'source_count':8,'source_live_count':4,'source_backtest_count':4,
             'foreign_key_count':12,'schema_count':14}
    monkeypatch.setattr(control,'_W3A_EVIDENCE',tmp_path)
    monkeypatch.setattr(control,'_W3A_BACKUP_SHA',backup_sha)
    monkeypatch.setattr(control,'_W3A_RESTORE_SHA',_save(tmp_path/'restore-proof.json',receipt))
    return tmp_path,receipt


def test_w3a_fixed_backup_proof_does_not_mix_w2(restored):
    backup,proof=control._backup('W3A')
    assert proof['backup_sha256']==control._W3A_BACKUP_SHA
    assert proof['source_database_identity_sha256']==backup['source_database_identity_sha256']
    assert proof['backup_sha256']!=control._BACKUP_SHA


@pytest.mark.parametrize('invalid',['bytes','scope','xor','partial','foreign_keys','digest','symlink'])
def test_w3a_restore_requires_full_eight_sources_and_immutable_originals(restored,monkeypatch,invalid):
    root,receipt=restored
    if invalid=='bytes': (root/'w3a-scoped-backup.json').write_text('{}')
    elif invalid=='symlink':
        (root/'w3a-scoped-backup.json').rename(root/'original.json')
        (root/'w3a-scoped-backup.json').symlink_to(root/'original.json')
    elif invalid=='scope':
        payload=json.loads((root/'w3a-scoped-backup.json').read_bytes())
        payload['scope'].append('unrelated')
        monkeypatch.setattr(control,'_W3A_BACKUP_SHA',_save(root/'w3a-scoped-backup.json',payload))
    else:
        field={'xor':'source_xor_actuals_verified','partial':'source_backtest_count','foreign_keys':'foreign_keys_enabled','digest':'records_sha256'}[invalid]
        receipt[field]=False
        monkeypatch.setattr(control,'_W3A_RESTORE_SHA',_save(root/'restore-proof.json',receipt))
    with pytest.raises((ValueError,OSError)):
        control._backup('W3A')


def test_backtest_summary_uses_its_historical_input_not_current_ready(historical):
    backup,path,reader=historical
    snapshot=backup['sources'][0]['run']['data_snapshot_id']
    backup['sources'].append({'backtest_run':{'summary':json.dumps({'data_snapshot_id':snapshot})}})
    inputs,_=control._historical_inputs(backup)
    assert set(inputs)=={snapshot}
    assert reader.call_count==1
    assert inputs[snapshot]['files']['daily_output.csv']==hashlib.sha256((path.parent/'data/daily_output.csv').read_bytes()).hexdigest()


@pytest.mark.parametrize('action',['preflight','preserve-live'])
def test_existing_cli_dispatches_w3a_without_fake_harness_or_rollback(monkeypatch,action):
    engine=MagicMock()
    monkeypatch.setattr(cli,'create_engine_from_env',lambda:engine)
    operation=MagicMock(return_value={'algorithm_executions':0})
    monkeypatch.setattr(cli,'build_w2_live_preservation_preflight' if action=='preflight' else 'execute_w2_live_preservation',operation)
    result=cli._run_native_successor_migration_command(cli._build_parser().parse_args(_args(action,'W3A')))
    assert result['algorithm_executions']==0
    assert operation.call_args.kwargs['wave']=='W3A'
    engine.dispose.assert_called_once()


@pytest.mark.parametrize('extra',[['--work-dir','/wrong'],['--rollback-project-root','/wrong'],['--harness-run-id','fake=run'],['--predict-date','2026-09-11']])
def test_w3a_preservation_rejects_unrelated_operation_options(monkeypatch,extra):
    engine=MagicMock(side_effect=AssertionError('no database access'))
    monkeypatch.setattr(cli,'create_engine_from_env',engine)
    with pytest.raises(ValueError):
        cli._run_native_successor_migration_command(cli._build_parser().parse_args(_args('preflight','W3A')+extra))
    engine.assert_not_called()
