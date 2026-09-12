"""剩余五方案单点迁移的封闭身份、历史保护与原事务回滚。"""
from copy import deepcopy
import os
from unittest.mock import patch

import pytest
from sqlalchemy import text

from scheduler import repository as repo
from test_same_id_runtime_upgrade import migration, _w3b_history_scope, _plan, _apply, _sqlite_upsert
from test_same_id_runtime_upgrade_mysql import mysql_migration


@pytest.fixture
def single(migration, monkeypatch):
    monkeypatch.setattr(repo, '_SAME_ID_W3B_NATIVE_HISTORY_IDS', repo._SAME_ID_SINGLE_REQUEST_WAVES['W3C'])
    engine, kwargs, evidence = _w3b_history_scope(migration)
    ids = sorted(kwargs['old_configs'])
    evidence.update(schema_version='single-request-migration-control-v1', wave='W3C', scheme_ids=ids,
        single_request_readiness={'schema_version':'single-request-migration-readiness-v1', 'scheme_ids':ids,
            'prepare_database_identity_sha256':repo.native_successor_plan_sha256({'isolated_test':'sqlite'}),
            'harness_run_ids':kwargs['harness_run_ids'], 'comparison_sha256':{key:'a'*64 for key in ids},
            'local_execution_sha256':{key:'b'*64 for key in ids}})
    return engine, kwargs, evidence


def test_single_request_cutover_and_rollback_preserve_all_facts(single):
    engine, kwargs, evidence = single
    before = _plan(engine, kwargs, evidence)
    with patch.object(repo, '_upsert_scheme_version_conn', side_effect=_sqlite_upsert):
        _apply(engine, kwargs, evidence, before)
        rollback = kwargs | {'action':'rollback'}
        after = _plan(engine, rollback, evidence)
        assert before['facts'] == after['facts']
        _apply(engine, rollback, evidence, after)
    final = _plan(engine, kwargs, evidence)
    assert final['facts'] == before['facts']
    assert not final['historical_native_versions_to_retire']


@pytest.mark.parametrize('drift',['db','comparison','execution','run','wave','scope','fence','canonical'])
def test_single_request_rejects_forged_readiness_or_identity(single, drift):
    engine, kwargs, evidence = single
    evidence = deepcopy(evidence)
    key = next(iter(kwargs['old_configs']))
    if drift == 'db': evidence['single_request_readiness']['prepare_database_identity_sha256']='f'*64
    elif drift in {'comparison','execution'}:
        field='comparison_sha256' if drift=='comparison' else 'local_execution_sha256'
        evidence['single_request_readiness'][field][key]='f'*64
    elif drift=='run': evidence['single_request_readiness']['harness_run_ids'][key]='fake'
    elif drift=='wave': evidence['wave']='W3D'
    elif drift=='scope': evidence['scheme_ids']=[key]
    elif drift=='fence': evidence['scheduler']['timer_fenced']=False
    elif drift=='canonical': evidence['native_canonical_selection'][key]['code_hash']='f'*64
    with pytest.raises((RuntimeError, ValueError)):
        _plan(engine, kwargs, evidence)


def test_prepare_is_read_only_without_backtest_or_gate_and_rejects_existing_attempt(single):
    engine, kwargs, evidence = single
    prepare = kwargs | {'action':'prepare','harness_run_ids':{}}
    with pytest.raises(RuntimeError, match='already attempted'):
        _plan(engine, prepare, evidence)
    with engine.begin() as conn:
        for key in kwargs['old_configs']:
            conn.execute(text('DELETE FROM t_harness_gate_results WHERE harness_run_id=:id'),{'id':key})
            conn.execute(text('DELETE FROM t_harness_runs WHERE harness_run_id=:id'),{'id':key})
    plan = _plan(engine, prepare, evidence)
    assert plan['action']=='prepare' and plan['evidence']=={}
    assert _plan(engine, prepare, evidence)==plan
    with pytest.raises(ValueError, match='cannot execute preparation'):
        _apply(engine, prepare, evidence, plan)


def test_single_selected_scheme_does_not_retire_other_native_versions(single):
    engine, kwargs, evidence = single
    key = sorted(kwargs['old_configs'])[0]
    kwargs = kwargs | {field:{key:kwargs[field][key]} for field in ('old_configs','new_configs','harness_run_ids')}
    evidence = deepcopy(evidence)
    evidence['scheme_ids']=[key]
    evidence['native_canonical_selection']={key:evidence['native_canonical_selection'][key]}
    evidence['single_request_readiness']['scheme_ids']=[key]
    before=_plan(engine,kwargs,evidence)
    with engine.connect() as conn:
        counts=conn.execute(text("SELECT COUNT(*) FROM t_scheme_versions WHERE scheme_id!=:id AND status='active'"),{'id':key}).scalar_one()
    with patch.object(repo,'_upsert_scheme_version_conn',side_effect=_sqlite_upsert):
        _apply(engine,kwargs,evidence,before)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM t_scheme_versions WHERE scheme_id!=:id AND status='active'"),{'id':key}).scalar_one()==counts


def test_late_member_failure_rolls_back_whole_selected_batch(single):
    engine, kwargs, evidence=single
    before=_plan(engine,kwargs,evidence)
    calls=[]
    def insert(conn,cfg,**options):
        calls.append(cfg.scheme_id)
        if len(calls)==2: raise RuntimeError('late test failure')
        return _sqlite_upsert(conn,cfg,**options)
    with patch.object(repo,'_upsert_scheme_version_conn',side_effect=insert), pytest.raises(RuntimeError,match='late test'):
        _apply(engine,kwargs,evidence,before)
    assert _plan(engine,kwargs,evidence)==before


@pytest.mark.skipif(not os.getenv('BFL_TEST_NATIVE_SUCCESSOR_MYSQL_URL'),reason='isolated MySQL required')
@pytest.mark.parametrize('fail_late',[False,True])
def test_mysql_single_request_real_transaction_and_historical_native_retirement(mysql_migration,monkeypatch,fail_late):
    monkeypatch.setattr(repo,'_SAME_ID_W3B_NATIVE_HISTORY_IDS',repo._SAME_ID_SINGLE_REQUEST_WAVES['W3C'])
    engine,kwargs,evidence=_w3b_history_scope(mysql_migration)
    ids=sorted(kwargs['old_configs'])
    with engine.connect() as conn:
        identity=dict(conn.execute(text('SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid')).mappings().one())
    evidence.update(schema_version='single-request-migration-control-v1',wave='W3C',scheme_ids=ids,
        single_request_readiness={'schema_version':'single-request-migration-readiness-v1','scheme_ids':ids,
            'prepare_database_identity_sha256':repo.native_successor_plan_sha256(identity),
            'harness_run_ids':kwargs['harness_run_ids'],'comparison_sha256':{key:'a'*64 for key in ids},
            'local_execution_sha256':{key:'b'*64 for key in ids}})
    before=_plan(engine,kwargs,evidence)
    if fail_late:
        original=repo._upsert_scheme_version_conn
        calls=[]
        def fail(conn,cfg,**options):
            calls.append(cfg.scheme_id)
            if len(calls)==2: raise RuntimeError('second member failed')
            return original(conn,cfg,**options)
        monkeypatch.setattr(repo,'_upsert_scheme_version_conn',fail)
        with pytest.raises(RuntimeError,match='second member'):
            _apply(engine,kwargs,evidence,before)
        assert _plan(engine,kwargs,evidence)==before
    else:
        _apply(engine,kwargs,evidence,before)
        rollback=kwargs|{'action':'rollback'}
        after=_plan(engine,rollback,evidence)
        assert before['facts']==after['facts']
        _apply(engine,rollback,evidence,after)
        final=_plan(engine,kwargs,evidence)
        assert final['facts']==before['facts'] and not final['historical_native_versions_to_retire']


def _approve_one_reentry(engine,kwargs,evidence,monkeypatch):
    prior=_plan(engine,kwargs,evidence)
    pins={key:(cfg.scheme_version,kwargs['harness_run_ids'][key],prior['evidence'][key]['sha256'],'c'*64)
          for key,cfg in kwargs['new_configs'].items()}
    monkeypatch.setattr(repo,'_SAME_ID_W3C_REENTRY',pins)
    evidence['candidate_seed_readiness']={key:{'published_state':{
        'scheme_version':cfg.scheme_version,'receipt_sha256':'c'*64,'verified':True,'additional_algorithm_executions':0}}
        for key,cfg in kwargs['new_configs'].items()}
    return _plan(engine,kwargs,evidence)


@pytest.fixture
def reentry(single,monkeypatch):
    engine,kwargs,evidence=single
    before=_approve_one_reentry(engine,kwargs,evidence,monkeypatch)
    with patch.object(repo,'_upsert_scheme_version_conn',side_effect=_sqlite_upsert):
        _apply(engine,kwargs,evidence,before)
        rollback=kwargs|{'action':'rollback'}
        _apply(engine,rollback,evidence,_plan(engine,rollback,evidence))
    return engine,kwargs|{'action':'prepare','harness_run_ids':{}},evidence


def test_exact_reviewed_retired_candidate_prepares_once_without_changing_old_gates(reentry):
    engine,kwargs,evidence=reentry
    before=_plan(engine,kwargs,evidence)
    assert before==_plan(engine,kwargs,evidence)
    key=next(iter(kwargs['new_configs']))
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO t_harness_runs SELECT 'second-attempt',scheme_id,scheme_version,code_hash,config_hash,stage,status,finished_at FROM t_harness_runs WHERE harness_run_id=:id"),{'id':key})
    with pytest.raises(RuntimeError,match='already attempted'):
        _plan(engine,kwargs,evidence)


@pytest.mark.parametrize('drift',['gate','run','simulation','verified','exact','active','execution'])
def test_reentry_rejects_any_unreviewed_harness_or_state(reentry,drift):
    engine,kwargs,evidence=reentry
    key=next(iter(kwargs['new_configs']))
    if drift=='gate':
        with engine.begin() as conn:
            conn.execute(text("UPDATE t_harness_gate_results SET summary_json='{}' WHERE harness_run_id=:id"),{'id':key})
    elif drift=='run':
        with engine.begin() as conn:
            conn.execute(text("UPDATE t_harness_runs SET status='failed' WHERE harness_run_id=:id"),{'id':key})
    elif drift=='active':
        with engine.begin() as conn:
            conn.execute(text("UPDATE t_scheme_versions SET status='active' WHERE scheme_id=:id AND scheme_version=:v"),{'id':key,'v':kwargs['new_configs'][key].scheme_version})
    else:
        field={'simulation':'receipt_sha256','verified':'verified','exact':'scheme_version','execution':'additional_algorithm_executions'}[drift]
        evidence['candidate_seed_readiness'][key]['published_state'][field]={'verified':False,'execution':1}.get(drift,'different')
    with pytest.raises(RuntimeError): _plan(engine,kwargs,evidence)


@pytest.mark.skipif(not os.getenv('BFL_TEST_NATIVE_SUCCESSOR_MYSQL_URL'),reason='isolated MySQL required')
def test_mysql_reentry_keeps_native_timestamp_gate_hash(mysql_migration,monkeypatch):
    monkeypatch.setattr(repo,'_SAME_ID_W3B_NATIVE_HISTORY_IDS',repo._SAME_ID_SINGLE_REQUEST_WAVES['W3C'])
    engine,kwargs,evidence=_w3b_history_scope(mysql_migration)
    ids=sorted(kwargs['old_configs'])
    with engine.connect() as conn:
        identity=dict(conn.execute(text('SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid')).mappings().one())
    evidence.update(schema_version='single-request-migration-control-v1',wave='W3C',scheme_ids=ids,
        single_request_readiness={'schema_version':'single-request-migration-readiness-v1','scheme_ids':ids,
            'prepare_database_identity_sha256':repo.native_successor_plan_sha256(identity),
            'harness_run_ids':kwargs['harness_run_ids'],'comparison_sha256':{key:'a'*64 for key in ids},
            'local_execution_sha256':{key:'b'*64 for key in ids}})
    before=_approve_one_reentry(engine,kwargs,evidence,monkeypatch)
    _apply(engine,kwargs,evidence,before)
    rollback=kwargs|{'action':'rollback'}
    _apply(engine,rollback,evidence,_plan(engine,rollback,evidence))
    prepared=_plan(engine,kwargs|{'action':'prepare','harness_run_ids':{}},evidence)
    assert prepared['facts']==before['facts']
