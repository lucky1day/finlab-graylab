"""剩余五个原 ID 的一次回归证据接入；只读就绪与 Harness，不运行算法或发布状态。"""
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from harness import same_id_runtime_upgrade as control
from harness.context import GateContext
from harness.persistence import new_harness_run_id, persist_harness_run_start, persist_harness_run_complete
from harness.result import Evidence, GateResult, GateStatus
from harness.w2_reclaim_prepare import _record_failed_gate
from harness.w3b_prepare import _write_receipt
from scheduler import repository as repo
from scheduler.blackbox_state import read_regular_bytes
from shared.blackbox_v2.contracts import load_request_bytes, result_from_mapping


_ROOT = Path(__file__).resolve().parents[1]
_EVIDENCE = Path('/opt/bond-factor-lab/incoming/final-five-single-request-20260912')
# 仅在原件完成后填入经独立审查的 SHA；未完成的方案始终拒绝准备。
_APPROVED_ACCEPTANCE_SHA = {
    'liwei_0616_5y_auc_static_all_k3_div_k10': '087c10858c2893a76facc34d090385e46a65b81ccbd7478a741f7f529490afb8',
    'liwei_0616_5y_auc_yearly_all_k3_div_k10': '7262f99dd3186f45262179c712a94d5be6d501a9198fba3483a9c125fba6a815',
    'liwei_0616_5y_ic_yearly_all_k3_div_k10': '1128e007c6be441d6a70109b1cb737b546cf7c78a871a2977051c262bf63bce5',
    'liwei_0616_7y01_cons_say_k3_div_k10': 'fc396a6fc86afea3e53d711f9b1fc7e8021ab8c8f6874887ae1d84774b44e16f',
    'liwei_0616_7y03_cons_all_k3_div_k8': 'ea7020f7bcd63a59ddfeba4e9ee01a4591b2b78de74f95d0cfe9a63e118b86e8',
}
_FIELDS = ('scheme_version', 'runtime_type', 'code_hash', 'config_hash', 'manifest_hash')
_STAGE = 'native-runtime-upgrade'
_APPROVED_ECS_SEED_SHA = {
    'liwei_0616_5y_auc_static_all_k3_div_k10': 'fd5e3307b346bc2440669005f4ca43746751cfa6d8bb6a561abfb168c1ea51e2',
    'liwei_0616_5y_auc_yearly_all_k3_div_k10': 'fe15f0e3a0b14b08f1cddc5692f369e3334d99f25b46314297faa4cbf0c61170',
    'liwei_0616_5y_ic_yearly_all_k3_div_k10': '75117fe5adec616d006c78cb3129da0cef14763096c52e4bb268464374391328',
}
_APPROVED_TIMEOUT_UPGRADES = {
    'liwei_0616_7y01_cons_say_k3_div_k10': ('79457f27982c', '4693f5417964',
        'b26b5527e281f97aec0af3b1ebeaaaa9b7dd3deaecdf5a42e7509ea2b9ba98c5'),
    'liwei_0616_7y03_cons_all_k3_div_k8': ('fdef50ae2219', '3ac41bdd36c6',
        'be323d1c6d30fe24243b180e33c3a4b57b5b7d0b1390edea043175b609ca2d95'),
}
_APPROVED_PUBLISHED_SIMULATIONS = {
    'liwei_0616_5y_auc_static_all_k3_div_k10': (
        '55d861318d647b3dc7ded0507f818fef9e07eaea371ebb2c7c6e34a9b3c38c8a',
        '1ec71b58443af34e2faa8111100ad41e32c29503e382f8cb4e6808a993438c34'),
    'liwei_0616_5y_auc_yearly_all_k3_div_k10': (
        '88a2ae29c93affa643e0507d4ab7166c3fd3bececb0d00c4f6e33c853705bca0',
        '3ab6f68a43fe4e0fbf7fc8413480117147872ec342399ae37ab708c9218d46e4'),
    'liwei_0616_5y_ic_yearly_all_k3_div_k10': (
        'e80f8af6dfa006e3e66b69e2bcfee44450706f69f88029a7e7a2edce09f38441',
        'a5eddbea3251b6b20c997f326603191abb08c7d10bef1d58569c0ba279ed6271'),
}


def _published_simulation(config, snapshot, input_files, destination, seed_sha):
    """重入只复用这三份成功 ECS 模拟发布的原封装，不运行算法或恢复旧 seed。"""
    from scheduler.blackbox_state import _decode, _MAX_ENVELOPE_BYTES
    from shared.blackbox_v2.contracts import load_prediction_result

    key = config.scheme_id
    pins = _APPROVED_PUBLISHED_SIMULATIONS[key]
    directory = _EVIDENCE / ('ecs-simulation-' + key)
    values = []
    for name, expected in zip(('receipt.json', 'started.json'), pins):
        raw = read_regular_bytes(directory / name, 4 * 1024**2)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise RuntimeError('approved published simulation evidence changed')
        values.append(json.loads(raw))
    receipt, started = values
    context = started['control']
    if (receipt.get('status') != 'passed' or receipt.get('scheme_id') != key
            or receipt.get('standard_predict_processes') != 1 or receipt.get('database_written') is not False
            or receipt.get('state_present_after') is not True or receipt.get('error') is not None
            or context['blackbox_environment_fingerprint'] != config.environment_fingerprint
            or context['databridge']['data_snapshot_id'] != snapshot.snapshot_id
            or context['databridge']['generation_id'] != snapshot.generation_id
            or context['databridge']['files'] != input_files or started['state_seed_sha256'] != seed_sha):
        raise RuntimeError('published simulation identity/input/environment differs')
    request_path = directory / 'request.json'
    request_raw = read_regular_bytes(request_path, 1024**2)
    if hashlib.sha256(request_raw).hexdigest() != started['closure_sha256'][str(request_path)]:
        raise RuntimeError('published simulation Request changed')
    request = load_request_bytes(request_raw)
    result_path = directory / 'output/result.json'
    if (hashlib.sha256(read_regular_bytes(result_path, 1024**2)).hexdigest() != receipt['result_sha256']
            or asdict(load_prediction_result(result_path, request)) != receipt['result']):
        raise RuntimeError('published simulation standard Result changed')
    raw = read_regular_bytes(destination, _MAX_ENVELOPE_BYTES)
    header, payload = _decode(raw)
    published = receipt['publication']
    if (hashlib.sha256(raw).hexdigest() != receipt['state_envelope_sha256']
            or receipt['state_envelope_sha256'] != published['state_envelope_sha256']
            or hashlib.sha256(payload).hexdigest() != published['state_output_sha256']
            or published['state_scope'] != 'persistent'
            or header['identity']['scheme_id'] != key or header['identity']['scheme_version'] != config.scheme_version
            or header['input']['files'] != input_files or header['input']['snapshot_id'] != snapshot.snapshot_id
            or header['input']['generation_id'] != snapshot.generation_id):
        raise RuntimeError('published simulation state envelope differs')
    checked = control._read_candidate_states(_ROOT, {key: config}, snapshot)[key]
    if checked['envelope_sha256'] != receipt['state_envelope_sha256'] or checked['payload_sha256'] != published['state_output_sha256']:
        raise RuntimeError('published simulation current StateSession identity differs')
    return {'receipt_sha256': pins[0], 'started_sha256': pins[1], 'verified': True,
            'scheme_version': config.scheme_version, 'envelope_sha256': receipt['state_envelope_sha256'],
            'result_sha256': receipt['result_sha256'], 'additional_algorithm_executions': 0}


def _acceptance_candidate_identity(proof, new):
    """W3D 唯一允许固定 exact 的 schedule.timeout_sec 120→300。"""
    current = {field: getattr(new, field) for field in _FIELDS}
    original = proof.get('candidate_identity')
    if original == current:
        return None
    allowed = _APPROVED_TIMEOUT_UPGRADES.get(new.scheme_id)
    if (not isinstance(original, dict) or allowed is None or allowed[:2] != (original.get('scheme_version'), new.scheme_version)
            or any(original.get(field) != current[field] for field in ('runtime_type', 'code_hash', 'manifest_hash'))):
        raise RuntimeError('single-Request candidate differs beyond approved timeout-only upgrade')
    import yaml
    from shared.blackbox_v2.versioning import compute_blackbox_config_hash
    from shared.versioning import compute_scheme_version
    raw_bytes = read_regular_bytes(new.path / 'config.yaml', 1024**2)
    if (raw_bytes.count(b'timeout_sec: 300') != 1
            or hashlib.sha256(raw_bytes.replace(b'timeout_sec: 300', b'timeout_sec: 120')).hexdigest() != allowed[2]):
        raise RuntimeError('timeout upgrade changed bytes beyond the fixed original config')
    raw = yaml.safe_load(raw_bytes)
    if type(raw['schedule']['timeout_sec']) is not int or raw['schedule']['timeout_sec'] != 300:
        raise RuntimeError('approved W3D timeout must be exactly 300 seconds')
    if (compute_blackbox_config_hash(raw) != new.config_hash
            or compute_scheme_version(new.code_hash, new.config_hash, new.manifest_hash) != new.scheme_version):
        raise RuntimeError('timeout upgrade canonical identity changed')
    raw['schedule']['timeout_sec'] = 120
    if compute_blackbox_config_hash(raw) != original['config_hash']:
        raise RuntimeError('candidate config changed beyond timeout 120 to 300')
    return {'old_identity': original, 'new_identity': current, 'only_change': 'schedule.timeout_sec:120->300',
            'original_config_sha256': allowed[2], 'current_config_sha256': hashlib.sha256(raw_bytes).hexdigest(),
            'additional_algorithm_executions': 0}


def load_ecs_seed(config, snapshot, input_files, *, require_unpublished):
    """只认经审核的 ECS 本地派生 seed；不预执行算法，不发布生产状态。"""
    from scheduler.blackbox_state import MAX_STATE_BYTES
    from scheduler.blackbox_v2_runner import state_binding_for_scheme

    key = config.scheme_id
    expected = _APPROVED_ECS_SEED_SHA.get(key)
    if key not in repo._SAME_ID_SINGLE_REQUEST_WAVES['W3C'] or expected is None:
        raise RuntimeError('ECS private seed has not been independently approved')
    directory = _EVIDENCE / key
    raw = read_regular_bytes(directory / 'ecs-seed.json', 4 * 1024**2)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise RuntimeError('approved ECS seed receipt changed')
    proof = json.loads(raw)
    if (proof.get('schema_version') != 'ecs-native-cache-seed-v1' or proof.get('scheme_id') != key
            or proof.get('candidate_identity') != {field:getattr(config,field) for field in _FIELDS}
            or proof.get('environment_fingerprint') != config.environment_fingerprint
            or proof.get('generation_id') != snapshot.generation_id
            or proof.get('data_snapshot_id') != snapshot.snapshot_id or proof.get('input_files') != input_files
            or proof.get('algorithm_executions') != 0 or proof.get('production_state_published') is not False
            or proof.get('source_kind') != 'ecs_native_cache' or not proof.get('source_cache')
            or not proof.get('preserved_cutoff')):
        raise RuntimeError('ECS private seed source, current input or candidate identity differs')
    payload = read_regular_bytes(directory / 'ecs-seed.npz', MAX_STATE_BYTES)
    if hashlib.sha256(payload).hexdigest() != proof.get('state_sha256'):
        raise RuntimeError('ECS private seed payload changed')
    binding = state_binding_for_scheme(config, generation_id=snapshot.generation_id, persistent=True)
    if binding is None or binding.root is None:
        raise RuntimeError('ECS private seed requires an explicit persistent exact state destination')
    destination = binding.root / key / f'{config.scheme_version}.state'
    published = None
    if key in _APPROVED_PUBLISHED_SIMULATIONS:
        published = _published_simulation(config, snapshot, input_files, destination, proof['state_sha256'])
    elif require_unpublished and os.path.lexists(destination):
        raise RuntimeError('candidate state already published before the sole post-cutover simulation')
    result = {'receipt_sha256':expected,'payload_sha256':proof['state_sha256'],
            'preserved_cutoff':proof['preserved_cutoff'],'source_kind':'ecs_native_cache',
            'destination':str(destination),'ecs_algorithm_executions':0,'production_state_published':False}
    if published is not None:
        result.update(published_state=published, production_state_published=True)
    return result


def load_acceptance(old, new):
    """固定 SHA 的有限单点验收，不解释为同输入或全历史等价。"""
    key = new.scheme_id
    expected = _APPROVED_ACCEPTANCE_SHA.get(key)
    if expected is None:
        raise RuntimeError('single-Request acceptance has not been independently approved')
    directory = _EVIDENCE / key
    raw = read_regular_bytes(directory / 'acceptance.json', 2 * 1024**2)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise RuntimeError('approved single-Request acceptance changed')
    proof = json.loads(raw)
    timeout_upgrade = _acceptance_candidate_identity(proof, new)
    if (proof.get('schema_version') != 'single-request-acceptance-v1' or proof.get('scheme_id') != key
            or proof.get('source_kind') not in {'existing_native_fact', 'prior_native_execution'}
            or proof.get('native_identity') != {field:getattr(old,field) for field in _FIELDS}
            or proof.get('same_input_equivalence') is not False or proof.get('historical_equivalence') is not False
            or set(proof.get('artifacts', {})) != {'native_evidence','candidate_execution','request','result'}):
        raise RuntimeError('single-Request evidence identity or limited comparison scope differs')
    artifacts = {}
    for name, item in proof['artifacts'].items():
        relative = Path(item['file'])
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise ValueError('single-Request evidence must remain inside its fixed directory')
        value = read_regular_bytes(directory / relative, 4 * 1024**2)
        if hashlib.sha256(value).hexdigest() != item['sha256']:
            raise RuntimeError('single-Request original artifact changed')
        artifacts[name] = value
    request = load_request_bytes(artifacts['request'])
    result = result_from_mapping(json.loads(artifacts['result']))
    if (request.request_id != ':'.join([key,request.predict_date,request.feature_date,request.target_date])
            or result.request_id != request.request_id
            or any(getattr(request,field) != getattr(result,field) for field in ('predict_date','feature_date','target_date'))
            or {field:getattr(result,field) for field in ('predict_date','feature_date','target_date','predicted_direction')}
            != proof.get('comparison_fields')):
        raise RuntimeError('standard single-Request dates/direction differ from approved Native baseline')
    native, execution = json.loads(artifacts['native_evidence']), json.loads(artifacts['candidate_execution'])
    if proof['source_kind']=='existing_native_fact':
        records=[row for row in native.get('records',[]) if row.get('scheme_id')==key]
        if (native.get('kind')!='existing_native_fact' or len(records)!=1
                or records[0].get('comparison_fields')!=proof['comparison_fields']
                or records[0].get('source',{}).get('scheme_version')!=old.scheme_version):
            raise RuntimeError('existing Native fact baseline differs')
    else:
        native_result=native.get('result',{})
        if {field:native_result.get(field) for field in proof['comparison_fields']} != proof['comparison_fields']:
            raise RuntimeError('prior Native execution baseline differs')
    if (execution.get('status') not in {'passed','standard_single_request_passed'}
            or execution.get('result')!=asdict(result) or execution.get('database_written') is not False
            or execution.get('standard_predict_processes',execution.get('algorithm_executions_requested'))!=1
            or execution.get('result_sha256',execution.get('output_sha256'))!=proof['artifacts']['result']['sha256']):
        raise RuntimeError('candidate standard single-Request execution receipt differs')
    result = {'acceptance_sha256':expected, 'source_kind':proof['source_kind'], 'request':asdict(request),
            'result':asdict(result), 'artifacts':proof['artifacts'], 'same_input_equivalence':False,
            'historical_equivalence':False, 'ecs_algorithm_executions':0}
    if timeout_upgrade is not None:
        result['timeout_only_upgrade'] = timeout_upgrade
    return result


def _selection(wave, values):
    allowed = repo._SAME_ID_SINGLE_REQUEST_WAVES.get(wave)
    ids = sorted(allowed if not values and allowed else values or [])
    if not allowed or not ids or len(ids) != len(set(ids)) or not set(ids) <= allowed:
        raise ValueError('select only original IDs from the fixed W3C/W3D wave')
    return ids


def _unchanged(engine, plan, owned_runs):
    """只排除本次明确拥有的 Harness 行；所有旧事实与 Registry/版本继续完整保护。"""
    ids = plan['scheme_ids']
    params = {f'id_{i}':key for i,key in enumerate(ids)}
    where = ','.join(':'+key for key in params)
    with engine.connect() as conn:
        facts = repo._same_id_fact_snapshot_conn(conn, ids, for_update=False)
        for table, order, scope in [('t_harness_runs','harness_run_id',f'scheme_id IN ({where})'),
                ('t_harness_gate_results','id',f'harness_run_id IN (SELECT harness_run_id FROM t_harness_runs WHERE scheme_id IN ({where}))')]:
            rows = repo._same_id_rows_conn(conn,table,scope,params,order=order,for_update=False)
            rows = [row for row in rows if row['harness_run_id'] not in owned_runs]
            facts[table] = {'count':len(rows),'sha256':repo.native_successor_plan_sha256({'rows':rows})}
        versions = repo._same_id_rows_conn(conn,'t_scheme_versions',f'scheme_id IN ({where})',params,order='scheme_id, scheme_version',for_update=False)
        registry = repo._same_id_rows_conn(conn,'t_scheme_registry',f'base_scheme_id IN ({where})',params,order='scheme_id',for_update=False)
        others = repo._same_id_rows_conn(conn,'t_scheme_registry',f'base_scheme_id NOT IN ({where})',params,order='scheme_id',for_update=False)
    if (facts != plan['facts'] or versions != plan['versions'] or registry != plan['registry']
            or repo.native_successor_plan_sha256({'rows':others}) != plan['unaffected_registry_sha256']):
        raise RuntimeError('single-Request preparation changed existing database facts or lifecycle')


def _prepare(engine, *, work_dir, expected_plan_sha256, approved_by, capture, database):
    if not approved_by or not approved_by.strip():
        raise ValueError('single-Request preparation requires an operator')
    work = Path(work_dir)
    if (not work.is_absolute() or work != work.resolve(strict=False) or os.path.lexists(work)
            or work.parent != _EVIDENCE):
        raise ValueError('single-Request preparation needs a fresh direct private work directory')
    old, new, _ = capture(True)
    ids = sorted(old)
    with ExitStack() as locks:
        for key in sorted(ids + [key+'_bbv2' for key in ids]):
            if engine.dialect.name != 'sqlite':
                locks.enter_context(repo._blackbox_activation_advisory_lock(engine, scheme_id=key))
        old, new, context = capture(True)
        plan = repo.read_same_id_runtime_upgrade_plan(engine,old_configs=old,new_configs=new,
            harness_run_ids={},action='prepare',control_plane_evidence=context,**database)
        if repo.native_successor_plan_sha256(plan) != expected_plan_sha256:
            raise RuntimeError('single-Request preparation plan drifted')
        work.mkdir(mode=0o700)
        _write_receipt(work/'plan.json',json.loads(repo.canonical_native_successor_plan(plan)))
        readiness = {'schema_version':'single-request-migration-readiness-v1','scheme_ids':ids,
            'prepare_database_identity_sha256':plan['database_identity_sha256'], 'context':context,
            'harness_run_ids':{},'comparison_sha256':{},'local_execution_sha256':{},'ecs_algorithm_executions':0}
        for key in ids:
            run_id, started = new_harness_run_id(), datetime.now(timezone.utc).isoformat()
            ctx = GateContext(key,context['acceptance'][key]['request']['predict_date'],_ROOT,config=new[key],engine_factory=lambda:engine)
            _write_receipt(work/(key+'.started.json'),{'harness_run_id':run_id,'started_at':started,'plan_sha256':expected_plan_sha256})
            readiness['harness_run_ids'][key] = run_id
            try:
                if not persist_harness_run_start(ctx,harness_run_id=run_id,stage=_STAGE,started_at=started):
                    raise RuntimeError('single-Request Harness start failed')
                local_sha = _write_receipt(work/(key+'.readiness.json'),{
                    'mac_single_request':context['acceptance'][key], 'ecs_read_only_context':context,
                    'ecs_algorithm_executions':0,'preparation_algorithm_executions':0})
                if capture(True)[2] != context:
                    raise RuntimeError('single-Request current/input/environment/state changed')
                _unchanged(engine,plan,set(readiness['harness_run_ids'].values()))
                proof = {'schema_version':'same-id-runtime-upgrade-evidence-v1','scheme_id':key,
                    'old_identity':{field:getattr(old[key],field) for field in _FIELDS},
                    'new_identity':{field:getattr(new[key],field) for field in _FIELDS},
                    'environment_fingerprint':new[key].environment_fingerprint,'data_snapshot_id':new[key].data_snapshot_id,
                    'generation_id':context['databridge']['generation_id'],
                    'equivalence_sha256':context['acceptance'][key]['acceptance_sha256'],'local_execution_sha256':local_sha}
                gate = GateResult(_STAGE,GateStatus.PASSED,[Evidence('runtime_upgrade',proof)],[],started,datetime.now(timezone.utc).isoformat())
                if not persist_harness_run_complete(ctx,harness_run_id=run_id,status='passed',finished_at=gate.finished_at,results=[gate]):
                    raise RuntimeError('single-Request Harness completion failed')
                readiness['comparison_sha256'][key] = proof['equivalence_sha256']
                readiness['local_execution_sha256'][key] = local_sha
            except BaseException as error:
                _record_failed_gate(engine,ctx,run_id,started,error)
                raise
        _unchanged(engine,plan,set(readiness['harness_run_ids'].values()))
        if capture(True)[2] != context:
            raise RuntimeError('single-Request context drifted after final Gate')
        _write_receipt(work/'readiness.json',readiness)
        return readiness


def run_single_request_command(args):
    """现有 CLI 的窄分派；不存在算法执行、状态发布、历史导入或清理选项。"""
    action = args.action if args.migration_action == 'preflight' else args.migration_action
    ids = _selection(args.wave,args.scheme_id)
    if (action not in {'prepare','cutover','rollback'} or args.rollback_project_root is not None
            or getattr(args,'predict_date',None) or (action=='prepare' and getattr(args,'harness_run_id',None))):
        raise ValueError('single-Request migration accepts only approved evidence and original-ID lifecycle operations')
    if args.work_dir is None:
        raise ValueError('single-Request migration requires its private preparation work-dir')
    engine = repo.create_engine_from_env()
    try:
        database = {'expected_database_name':args.expected_database_name,'expected_server_uuid':args.expected_server_uuid}
        def capture(preparing):
            return control.capture_single_request(engine,project_root=args.project_root,reference_project_root=args.reference_project_root,
                wave=args.wave,scheme_ids=ids,preparing=preparing,rollback=action=='rollback')
        if action=='prepare' and args.migration_action!='preflight':
            return _prepare(engine,work_dir=args.work_dir,expected_plan_sha256=args.expected_plan_sha256,
                approved_by=args.approved_by,capture=capture,database=database)
        def controlled():
            old,new,context = capture(action=='prepare')
            runs = {}
            if action!='prepare':
                work=Path(args.work_dir)
                if work.parent != _EVIDENCE or work != work.resolve(strict=True):
                    raise ValueError('preparation directory outside fixed private evidence root')
                ready=json.loads(read_regular_bytes(work/'readiness.json',4*1024**2))
                if ready['context'] != context or ready['scheme_ids'] != ids or ready.get('ecs_algorithm_executions') != 0:
                    raise RuntimeError('single-Request readiness differs from current read-only context')
                for key in ids:
                    if hashlib.sha256(read_regular_bytes(work/(key+'.readiness.json'),4*1024**2)).hexdigest()!=ready['local_execution_sha256'][key]:
                        raise RuntimeError('single-Request local readiness artifact changed')
                runs=ready['harness_run_ids']
                supplied={value.partition('=')[0]:value.partition('=')[2] for value in args.harness_run_id or []}
                if len(args.harness_run_id or []) != len(ids) or supplied != runs:
                    raise ValueError('single-Request Harness IDs differ from preparation')
                context=context | {'single_request_readiness':ready}
            return old,new,context,runs
        old,new,context,runs=controlled()
        options=dict(old_configs=old,new_configs=new,harness_run_ids=runs,action=action,**database)
        if args.migration_action=='preflight':
            plan=repo.read_same_id_runtime_upgrade_plan(engine,**options,control_plane_evidence=context)
            return {'plan':plan,'plan_sha256':repo.native_successor_plan_sha256(plan)}
        return repo.apply_same_id_runtime_upgrade(engine,**options,expected_plan_sha256=args.expected_plan_sha256,
            approved_by=args.approved_by,approved_at=datetime.now(timezone.utc),
            additional_lifecycle_lock_scheme_ids=tuple(key+'_bbv2' for key in ids),
            control_plane_evidence_reader=lambda:controlled()[2])
    finally:
        engine.dispose()
