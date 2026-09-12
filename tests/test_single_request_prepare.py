"""一次回归原件、CLI 选择、零算法准备及原审计保护。"""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from harness import cli, single_request_prepare as preparation
from scheduler import repository as repo
from test_single_request_migration import single, migration


def save(path, value):
    raw=json.dumps(value,sort_keys=True).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def acceptance(tmp_path, monkeypatch):
    key=sorted(repo._SAME_ID_SINGLE_REQUEST_WAVES['W3D'])[0]
    directory=tmp_path/key
    directory.mkdir()
    old=SimpleNamespace(scheme_id=key,scheme_version='old',runtime_type='native_adapter',code_hash='a'*64,config_hash='b'*64,manifest_hash=None)
    new=SimpleNamespace(scheme_id=key,scheme_version='new',runtime_type='blackbox_v2',code_hash='c'*64,config_hash='d'*64,manifest_hash='e'*64)
    request={'request_id':key+':2026-08-25:2026-08-24:2026-08-31','predict_date':'2026-08-25',
        'feature_date':'2026-08-24','target_date':'2026-08-31','daily_cutoff_key':'2026-08-24',
        'weekly_cutoff_key':'202635','monthly_cutoff_key':'202608'}
    result={field:request[field] for field in ('request_id','predict_date','feature_date','target_date')}|{'predicted_direction':1}
    fields={field:result[field] for field in ('predict_date','feature_date','target_date','predicted_direction')}
    files={'request':request,'result':result,'native_evidence':{'kind':'existing_native_fact','records':[
        {'scheme_id':key,'source':{'scheme_version':'old'},'comparison_fields':fields}]}}
    artifacts={name:{'file':name+'.json','sha256':save(directory/(name+'.json'),value)} for name,value in files.items()}
    execution={'status':'passed','result':result,'algorithm_executions_requested':1,
        'output_sha256':artifacts['result']['sha256'],'database_written':False}
    artifacts['candidate_execution']={'file':'candidate_execution.json','sha256':save(directory/'candidate_execution.json',execution)}
    proof={'schema_version':'single-request-acceptance-v1','scheme_id':key,'source_kind':'existing_native_fact',
        'native_identity':{field:getattr(old,field) for field in preparation._FIELDS},
        'candidate_identity':{field:getattr(new,field) for field in preparation._FIELDS},
        'comparison_fields':fields,'same_input_equivalence':False,'historical_equivalence':False,'artifacts':artifacts}
    monkeypatch.setattr(preparation,'_EVIDENCE',tmp_path)
    monkeypatch.setattr(preparation,'_APPROVED_ACCEPTANCE_SHA',{key:save(directory/'acceptance.json',proof)})
    return old,new,proof,directory


def test_frozen_existing_fact_is_not_faked_as_same_input_or_ecs_execution(acceptance):
    old,new,_,_=acceptance
    result=preparation.load_acceptance(old,new)
    assert result['source_kind']=='existing_native_fact'
    assert result['historical_equivalence'] is result['same_input_equivalence'] is False
    assert result['ecs_algorithm_executions']==0


@pytest.mark.parametrize('drift',['pin','candidate','native','scope','path','bytes','result','duplicate_process','baseline'])
def test_single_request_artifacts_fail_closed(acceptance,drift,monkeypatch):
    old,new,proof,directory=acceptance
    if drift=='pin': preparation._APPROVED_ACCEPTANCE_SHA.clear()
    elif drift=='candidate': new.code_hash='f'*64
    elif drift=='native': old.scheme_version='different'
    elif drift=='scope': proof['historical_equivalence']=True
    elif drift=='path': proof['artifacts']['request']['file']='../outside.json'
    elif drift=='bytes': (directory/'request.json').write_text('{}')
    elif drift in {'result','duplicate_process'}:
        path=directory/'candidate_execution.json'; value=json.loads(path.read_bytes())
        if drift=='result': value['result']['predicted_direction']=-1
        else: value['algorithm_executions_requested']=2
        proof['artifacts']['candidate_execution']['sha256']=save(path,value)
    else:
        path=directory/'native_evidence.json'; value=json.loads(path.read_bytes())
        value['records'][0]['comparison_fields']['predicted_direction']=-1
        proof['artifacts']['native_evidence']['sha256']=save(path,value)
    if drift not in {'pin','candidate','native','bytes'}:
        preparation._APPROVED_ACCEPTANCE_SHA[new.scheme_id]=save(directory/'acceptance.json',proof)
    with pytest.raises((RuntimeError,ValueError)):
        preparation.load_acceptance(old,new)


def test_prior_native_execution_keeps_original_request_identity(acceptance):
    old,new,proof,directory=acceptance
    proof['source_kind']='prior_native_execution'
    native={'result':proof['comparison_fields']|{'request_id':'historical-native-identity'}}
    proof['artifacts']['native_evidence']['sha256']=save(directory/'native_evidence.json',native)
    preparation._APPROVED_ACCEPTANCE_SHA[new.scheme_id]=save(directory/'acceptance.json',proof)
    assert preparation.load_acceptance(old,new)['source_kind']=='prior_native_execution'


@pytest.mark.parametrize('wave',['W3C','W3D'])
def test_cli_selected_original_and_closed_operations(wave,monkeypatch):
    key=sorted(repo._SAME_ID_SINGLE_REQUEST_WAVES[wave])[0]
    command=['migrate-native-successor','preflight','--wave',wave,'--scheme-id',key,'--action','prepare',
        '--reference-project-root','/reference','--expected-database-name','isolated','--expected-server-uuid','isolated','--work-dir','/private']
    operation=MagicMock(return_value={'zero_algorithms':True})
    monkeypatch.setattr(preparation,'run_single_request_command',operation)
    assert cli._run_native_successor_migration_command(cli._build_parser().parse_args(command))['zero_algorithms']
    assert operation.call_args.args[0].scheme_id==[key]
    assert preparation._selection(wave,[key])==[key]
    with pytest.raises(ValueError): preparation._selection(wave,[key,key])
    with pytest.raises(ValueError): preparation._selection(wave,['unrelated'])


@pytest.fixture
def prepare_case(single,tmp_path,monkeypatch):
    engine,kwargs,context=single
    context=deepcopy(context)
    context.pop('single_request_readiness')
    context['acceptance']={key:{'request':{'predict_date':'2026-09-12'},'acceptance_sha256':'a'*64} for key in kwargs['old_configs']}
    with engine.begin() as conn:
        for key in kwargs['old_configs']:
            conn.execute(text('DELETE FROM t_harness_gate_results WHERE harness_run_id=:id'),{'id':key})
            conn.execute(text('DELETE FROM t_harness_runs WHERE harness_run_id=:id'),{'id':key})
    def start(ctx,**kw):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO t_harness_runs VALUES (:id,:scheme,:version,:code,:config,'native-runtime-upgrade','running',NULL)"),
                {'id':kw['harness_run_id'],'scheme':ctx.scheme_id,'version':ctx.config.scheme_version,'code':ctx.config.code_hash,'config':ctx.config.config_hash})
        return True
    def complete(ctx,**kw):
        gate=kw['results'][0]
        with engine.begin() as conn:
            conn.execute(text('UPDATE t_harness_runs SET status=:status,finished_at=:finished WHERE harness_run_id=:id'),
                {'status':kw['status'],'finished':kw['finished_at'],'id':kw['harness_run_id']})
            conn.execute(text('INSERT INTO t_harness_gate_results (harness_run_id,gate_name,status,finished_at,summary_json) VALUES (:id,:gate,:status,:finished,:summary)'),
                {'id':kw['harness_run_id'],'gate':gate.gate_name,'status':kw['status'],'finished':kw['finished_at'],
                 'summary':json.dumps({'passed':kw['status']=='passed','errors':gate.errors,'evidence':[{'key':e.key,'value':e.value} for e in gate.evidence]})})
        return True
    monkeypatch.setattr(preparation,'_EVIDENCE',tmp_path)
    monkeypatch.setattr(preparation,'persist_harness_run_start',start)
    monkeypatch.setattr(preparation,'persist_harness_run_complete',complete)
    from harness import w2_reclaim_prepare
    monkeypatch.setattr(w2_reclaim_prepare,'persist_harness_run_complete',complete)
    capture=lambda preparing:(kwargs['old_configs'],kwargs['new_configs'],context)
    database={field:kwargs[field] for field in ('expected_database_name','expected_server_uuid')}
    plan=repo.read_same_id_runtime_upgrade_plan(engine,old_configs=kwargs['old_configs'],new_configs=kwargs['new_configs'],harness_run_ids={},action='prepare',control_plane_evidence=context,**database)
    options={'work_dir':tmp_path/'work','expected_plan_sha256':repo.native_successor_plan_sha256(plan),
             'approved_by':'test','capture':capture,'database':database}
    return engine,kwargs,context,plan,options


def test_prepare_creates_only_true_harness_no_algorithm_or_lifecycle(prepare_case):
    engine,kwargs,context,plan,options=prepare_case
    result=preparation._prepare(engine,**options)
    assert result['ecs_algorithm_executions']==0 and len(result['harness_run_ids'])==3
    preparation._unchanged(engine,plan,set(result['harness_run_ids'].values()))
    with pytest.raises(ValueError,match='fresh'):
        preparation._prepare(engine,**options)


def test_prepare_rejects_stale_plan_before_any_audit_or_directory(prepare_case):
    engine,kwargs,context,plan,options=prepare_case
    options['expected_plan_sha256']='f'*64
    with pytest.raises(RuntimeError,match='drifted'):
        preparation._prepare(engine,**options)
    assert not options['work_dir'].exists()
    preparation._unchanged(engine,plan,set())


@pytest.mark.parametrize('committed',[False,True])
def test_failed_preparation_preserves_original_error_and_committed_gate(prepare_case,monkeypatch,committed):
    engine,_,_,plan,options=prepare_case
    complete=preparation.persist_harness_run_complete
    error=RuntimeError('original persistence outcome')
    def interrupted(ctx,**kw):
        if committed: complete(ctx,**kw)
        raise error
    monkeypatch.setattr(preparation,'persist_harness_run_complete',interrupted)
    with pytest.raises(RuntimeError) as caught: preparation._prepare(engine,**options)
    assert caught.value is error
    with engine.connect() as conn:
        rows=conn.execute(text("SELECT status FROM t_harness_runs WHERE scheme_id=:id"),
            {'id':plan['scheme_ids'][0]}).scalars().all()
        gates=conn.execute(text("SELECT status FROM t_harness_gate_results")).scalars().all()
    assert rows==['passed' if committed else 'failed']
    assert gates.count('passed' if committed else 'failed')>=1
    assert not (options['work_dir']/'readiness.json').exists()


@pytest.fixture
def ecs_seed(tmp_path,monkeypatch):
    from scheduler import blackbox_v2_runner
    key=sorted(repo._SAME_ID_SINGLE_REQUEST_WAVES['W3C'])[0]
    cfg=SimpleNamespace(scheme_id=key,scheme_version='new',runtime_type='blackbox_v2',code_hash='c'*64,
        config_hash='d'*64,manifest_hash='e'*64,environment_fingerprint='f'*64)
    snapshot=SimpleNamespace(generation_id='generation',snapshot_id='snapshot')
    files={name:'a'*64 for name in ('daily','weekly','monthly','factors','calendar')}
    directory=tmp_path/key;directory.mkdir()
    payload=b'fixed reviewed private seed bytes'
    (directory/'ecs-seed.npz').write_bytes(payload)
    proof={'schema_version':'ecs-native-cache-seed-v1','scheme_id':key,
        'candidate_identity':{field:getattr(cfg,field) for field in preparation._FIELDS},
        'environment_fingerprint':cfg.environment_fingerprint,'generation_id':snapshot.generation_id,
        'data_snapshot_id':snapshot.snapshot_id,'input_files':files,'algorithm_executions':0,
        'production_state_published':False,'source_kind':'ecs_native_cache','source_cache':{'manifest_sha256':'a'*64},
        'preserved_cutoff':'2026-09-09','state_sha256':hashlib.sha256(payload).hexdigest()}
    monkeypatch.setattr(preparation,'_EVIDENCE',tmp_path)
    monkeypatch.setattr(preparation,'_APPROVED_ECS_SEED_SHA',{key:save(directory/'ecs-seed.json',proof)})
    destination=tmp_path/'runtime'/key/'new.state'
    monkeypatch.setattr(blackbox_v2_runner,'state_binding_for_scheme',lambda *a,**k:SimpleNamespace(root=tmp_path/'runtime'))
    return cfg,snapshot,files,directory,proof,destination


def test_ecs_seed_is_read_only_and_rollback_keeps_first_simulation_state(ecs_seed):
    cfg,snapshot,files,_,_,destination=ecs_seed
    before=preparation.load_ecs_seed(cfg,snapshot,files,require_unpublished=True)
    assert not destination.parent.exists()
    destination.parent.mkdir(parents=True);destination.write_bytes(b'first validated simulation state')
    with pytest.raises(RuntimeError,match='already published'):
        preparation.load_ecs_seed(cfg,snapshot,files,require_unpublished=True)
    assert preparation.load_ecs_seed(cfg,snapshot,files,require_unpublished=False)==before
    assert destination.read_bytes()==b'first validated simulation state'


@pytest.mark.parametrize('drift',['unapproved','receipt','payload','input','runtime','candidate'])
def test_ecs_seed_never_accepts_mac_or_stale_bytes(ecs_seed,drift):
    cfg,snapshot,files,directory,proof,_=ecs_seed
    if drift=='unapproved': preparation._APPROVED_ECS_SEED_SHA.clear()
    elif drift=='receipt': (directory/'ecs-seed.json').write_text('{}')
    elif drift=='payload': (directory/'ecs-seed.npz').write_bytes(b'changed')
    elif drift=='input': files={**files,'daily':'b'*64}
    elif drift=='runtime': cfg.environment_fingerprint='0'*64
    else: cfg.scheme_version='other'
    with pytest.raises(RuntimeError): preparation.load_ecs_seed(cfg,snapshot,files,require_unpublished=True)
