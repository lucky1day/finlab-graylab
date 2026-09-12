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
    monkeypatch.setattr(preparation,'_APPROVED_PUBLISHED_SIMULATIONS',{})
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


@pytest.fixture
def timeout_upgrade(acceptance,monkeypatch):
    import yaml
    from shared.blackbox_v2.versioning import compute_blackbox_config_hash
    from shared.versioning import compute_scheme_version
    old,new,proof,directory=acceptance
    raw={'scheme_id':new.scheme_id,'runtime_type':'blackbox_v2','input_source':'data_bridge_current',
        'runtime_profile':'blackbox-v2-v1','data_schema_version':'data-bridge-v1',
        'delivery':{'script':'delivery.py','metadata':'delivery.json'},
        'schedule':{'cron':'3 7 * * 1-5','timezone':'Asia/Shanghai','timeout_sec':120}}
    prior=yaml.safe_dump(raw).encode()
    proof['candidate_identity']['config_hash']=compute_blackbox_config_hash(raw)
    raw['schedule']['timeout_sec']=300
    new.path=directory/'candidate';new.path.mkdir()
    (new.path/'config.yaml').write_text(yaml.safe_dump(raw))
    new.config_hash=compute_blackbox_config_hash(raw)
    new.scheme_version=compute_scheme_version(new.code_hash,new.config_hash,new.manifest_hash)
    monkeypatch.setattr(preparation,'_APPROVED_TIMEOUT_UPGRADES',{new.scheme_id:('new',new.scheme_version,hashlib.sha256(prior).hexdigest())})
    preparation._APPROVED_ACCEPTANCE_SHA[new.scheme_id]=save(directory/'acceptance.json',proof)
    return old,new,proof,directory


def test_only_fixed_timeout_change_reuses_original_bytes(timeout_upgrade):
    old,new,proof,directory=timeout_upgrade
    before=(directory/'acceptance.json').read_bytes()
    accepted=preparation.load_acceptance(old,new)
    assert accepted['timeout_only_upgrade']['only_change']=='schedule.timeout_sec:120->300'
    assert accepted['timeout_only_upgrade']['additional_algorithm_executions']==0
    assert (directory/'acceptance.json').read_bytes()==before


@pytest.mark.parametrize('drift',['timeout','unhashed','code','meta','exact','config'])
def test_timeout_change_does_not_relax_any_other_candidate_bytes(timeout_upgrade,drift):
    old,new,proof,directory=timeout_upgrade
    if drift in {'timeout','unhashed'}:
        path=new.path/'config.yaml'
        path.write_text(path.read_text().replace('timeout_sec: 300','timeout_sec: 301') if drift=='timeout' else path.read_text()+'status: active\n')
    else:
        setattr(new,{'code':'code_hash','meta':'manifest_hash','exact':'scheme_version','config':'config_hash'}[drift],'f'*64)
    with pytest.raises(RuntimeError): preparation.load_acceptance(old,new)


@pytest.fixture
def published_seed(ecs_seed,monkeypatch):
    from scheduler.blackbox_state import _encode
    cfg,snapshot,files,directory,seed,destination=ecs_seed
    simulation=directory.parent/('ecs-simulation-'+cfg.scheme_id)
    (simulation/'output').mkdir(parents=True)
    request={'request_id':cfg.scheme_id+':2026-09-12:2026-09-11:2026-09-18','predict_date':'2026-09-12',
        'feature_date':'2026-09-11','target_date':'2026-09-18','daily_cutoff_key':'2026-09-11',
        'weekly_cutoff_key':'202637','monthly_cutoff_key':'202609'}
    result={field:request[field] for field in ('request_id','predict_date','feature_date','target_date')}|{'predicted_direction':1}
    request_sha=save(simulation/'request.json',request)
    result_sha=save(simulation/'output/result.json',result)
    payload=b'completed real-state fixture'
    payload_sha=hashlib.sha256(payload).hexdigest()
    header={'identity':{'scheme_id':cfg.scheme_id,'scheme_version':cfg.scheme_version},
        'input':{'files':files,'snapshot_id':snapshot.snapshot_id,'generation_id':snapshot.generation_id},'payload_sha256':payload_sha}
    state=_encode(header,payload);state_sha=hashlib.sha256(state).hexdigest()
    destination.parent.mkdir(parents=True);destination.write_bytes(state)
    receipt={'status':'passed','scheme_id':cfg.scheme_id,'standard_predict_processes':1,'database_written':False,
        'state_present_after':True,'error':None,'result':result,'result_sha256':result_sha,'state_envelope_sha256':state_sha,
        'publication':{'state_envelope_sha256':state_sha,'state_output_sha256':payload_sha,'state_scope':'persistent'}}
    started={'state_seed_sha256':seed['state_sha256'],'closure_sha256':{str(simulation/'request.json'):request_sha},
        'control':{'blackbox_environment_fingerprint':cfg.environment_fingerprint,
            'databridge':{'data_snapshot_id':snapshot.snapshot_id,'generation_id':snapshot.generation_id,'files':files}}}
    pins=(save(simulation/'receipt.json',receipt),save(simulation/'started.json',started))
    monkeypatch.setattr(preparation,'_APPROVED_PUBLISHED_SIMULATIONS',{cfg.scheme_id:pins})
    monkeypatch.setattr(preparation.control,'_read_candidate_states',lambda *a:{cfg.scheme_id:{'envelope_sha256':state_sha,'payload_sha256':payload_sha}})
    return ecs_seed,simulation,receipt


def test_reentry_uses_only_reviewed_published_state_without_republish(published_seed):
    (cfg,snapshot,files,_,_,destination),_,_=published_seed
    before=destination.read_bytes()
    check=preparation.load_ecs_seed(cfg,snapshot,files,require_unpublished=True)
    assert check['published_state']['verified'] and check['production_state_published']
    assert check['published_state']['additional_algorithm_executions']==0
    assert preparation.load_ecs_seed(cfg,snapshot,files,require_unpublished=False)==check
    assert destination.read_bytes()==before


@pytest.mark.parametrize('drift',['receipt','state','deleted','request','result','runtime','input','exact'])
def test_published_reentry_never_falls_back_or_reexecutes(published_seed,drift,monkeypatch):
    (cfg,snapshot,files,_,_,destination),simulation,receipt=published_seed
    if drift=='deleted': destination.unlink()
    elif drift in {'receipt','request','result'}:
        (simulation/({'receipt':'receipt.json','request':'request.json','result':'output/result.json'}[drift])).write_text('{}')
    elif drift=='state': destination.write_bytes(b'changed')
    elif drift=='runtime': monkeypatch.setattr(preparation.control,'_read_candidate_states',lambda *a:{cfg.scheme_id:{'envelope_sha256':'f'*64,'payload_sha256':'f'*64}})
    elif drift=='input': files={**files,'daily':'b'*64}
    else: cfg.scheme_version='different'
    with pytest.raises((RuntimeError,ValueError,FileNotFoundError)):
        preparation.load_ecs_seed(cfg,snapshot,files,require_unpublished=True)
