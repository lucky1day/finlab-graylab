"""输入审计不阻断、原始数据不变、历史截止及运行日志公共合同。"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from shared.prediction_input_quality import check_prediction_input_quality, audit_prediction_input_quality
from shared.prediction_audit import emit_prediction_audit, audit_run_context


def setup_case(tmp_path):
    script=tmp_path/'model.py';script.write_text('# immutable delivery\n')
    data=tmp_path/'data';data.mkdir()
    dates=pd.bdate_range('2026-09-01',periods=25).strftime('%Y-%m-%d')
    frame=pd.DataFrame({'date':dates,'required':range(25),'optional':[None]*25})
    frame.to_csv(data/'daily_output.csv',index=False)
    rule={'scheme_version':'exact1','script_sha256':hashlib.sha256(script.read_bytes()).hexdigest(),'requirements':[{'filename':'daily_output.csv','columns':['required'],'offset':1,'rows':11,'min_valid':11,'mode':'block'}]}
    policy=tmp_path/'policy.json';policy.write_text(json.dumps({'schema_version':'prediction-input-quality-v1','schemes':{'model':rule}}))
    return SimpleNamespace(scheme_id='model',scheme_version='exact1'),script,data,SimpleNamespace(predict_date='2026-10-06',feature_date=dates[-1],daily_cutoff_key=dates[-1]),policy,frame


def test_missing_values_are_logged_without_mutating_or_blocking_input(tmp_path, monkeypatch):
    cfg,script,data,request,policy,frame=setup_case(tmp_path)
    assert check_prediction_input_quality(cfg,script,data,request,policy_path=policy)['status']=='complete'
    frame.loc[23,'required']=None;frame.to_csv(data/'daily_output.csv',index=False)
    before=(data/'daily_output.csv').read_bytes()
    report=check_prediction_input_quality(cfg,script,data,request,policy_path=policy)
    assert report['status']=='incomplete' and report['enforcement']=='log_only'
    finding=report['findings'][0]
    assert finding['dates']==[frame.loc[23,'date']]
    assert finding['last_available_observation']=={'snapshot_axis':frame.loc[22,'date'],'value':22.0}
    assert finding['actual_used_observation']=='not_exposed_by_delivery'
    assert before==(data/'daily_output.csv').read_bytes()
    monkeypatch.setenv('BFL_RUNTIME_ROOT',str(tmp_path/'runtime'))
    with audit_run_context(123):
        reference=audit_prediction_input_quality(cfg,script,data,request,policy_path=policy,snapshot_id='sealed')
    log=tmp_path/'runtime/logs'/reference['audit_log']
    event=json.loads(log.read_text())
    assert event['run_id']==123 and event['data_snapshot_id']=='sealed'
    assert event['report']['findings']==report['findings']


@pytest.mark.parametrize('failure',['exact','script','undeclared','policy_broken','missing_file','missing_column','cutoff'])
def test_audit_explains_checks_without_preventing_model_execution(tmp_path,failure):
    cfg,script,data,request,policy,frame=setup_case(tmp_path)
    if failure=='exact':cfg.scheme_version='other'
    elif failure=='script':script.write_text('# other\n')
    elif failure=='undeclared':cfg.scheme_id='not_declared'
    elif failure=='policy_broken':policy.write_text('{broken')
    elif failure=='missing_file':(data/'daily_output.csv').unlink()
    elif failure=='missing_column':frame.drop(columns=['required']).to_csv(data/'daily_output.csv',index=False)
    else:request.daily_cutoff_key=request.feature_date='2026-10-31'
    report=check_prediction_input_quality(cfg,script,data,request,policy_path=policy)
    assert report['status']!='complete' and report['findings']
    assert report['enforcement']=='log_only'


def test_future_rows_never_fill_past_holes_and_zero_is_valid(tmp_path):
    cfg,script,data,request,policy,frame=setup_case(tmp_path)
    frame.loc[23,'required']=None
    frame.loc[len(frame)]=['2026-10-30',999,None];frame.to_csv(data/'daily_output.csv',index=False)
    report=check_prediction_input_quality(cfg,script,data,request,policy_path=policy)
    assert report['status']=='incomplete'
    assert report['findings'][0]['last_available_observation']['value']==22


def test_period_cutoff_uses_its_own_axis_and_records_value_age(tmp_path):
    cfg,script,data,request,policy,frame=setup_case(tmp_path)
    obj=json.loads(policy.read_text());obj['schemes']['model']['requirements']=[{'filename':'weekly_output.csv','columns':['w'],'rows':2,'min_valid':2,'mode':'block'}];policy.write_text(json.dumps(obj))
    pd.DataFrame({'week_id':['202640','202641','202642','202643'],'w':[1,2,None,99]}).to_csv(data/'weekly_output.csv',index=False)
    request.weekly_cutoff_key='202642'
    report=check_prediction_input_quality(cfg,script,data,request,policy_path=policy)
    assert report['findings'][0]['dates']==['202642']
    assert report['findings'][0]['last_available_observation']=={'snapshot_axis':'202641','value':2.0}


def test_batch_keeps_one_input_view_and_new_batch_observes_new_inputs(tmp_path):
    """批次内复用已读取输入，后续批次不会继承旧策略或数据。"""
    from shared.prediction_input_quality import PredictionInputAuditBatch

    cfg, script, data, request, policy, frame = setup_case(tmp_path)
    batch = PredictionInputAuditBatch(data, policy_path=policy)
    first = check_prediction_input_quality(cfg, script, data, request, batch=batch)
    assert first['status'] == 'complete'
    (data/'daily_output.csv').unlink()
    policy.write_text('{broken')
    second = check_prediction_input_quality(cfg, script, data, request, batch=batch)
    assert second == first
    later = check_prediction_input_quality(
        cfg, script, data, request,
        batch=PredictionInputAuditBatch(data, policy_path=policy),
    )
    assert later['status'] == 'check_error'
    assert later['coverage'] == 'unverified'


def test_batch_audits_each_cutoff_and_rejects_another_input_directory(tmp_path):
    """复用读取不能复用某天结论，也不能混用另一个输入目录。"""
    from shared.prediction_input_quality import PredictionInputAuditBatch

    cfg, script, data, request, policy, frame = setup_case(tmp_path)
    batch = PredictionInputAuditBatch(data, policy_path=policy)
    first = check_prediction_input_quality(cfg, script, data, request, batch=batch)
    request.daily_cutoff_key = request.feature_date = frame.iloc[4]['date']
    earlier = check_prediction_input_quality(cfg, script, data, request, batch=batch)
    assert first['status'] == 'complete'
    assert earlier['status'] == 'incomplete'
    assert earlier['checked_windows'][0]['observed_end'] == frame.iloc[3]['date']
    other = check_prediction_input_quality(cfg, script, tmp_path/'other', request, batch=batch)
    assert other['status'] == 'check_error'


def test_concurrent_jsonl_events_are_complete_and_never_create_directions(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('BFL_RUNTIME_ROOT',str(tmp_path))
    def write(n):
        with audit_run_context(n):
            return emit_prediction_audit('prediction_result',scheme_id='m',predict_date='2026-10-29',
                                         status='failed',outcome='not_computable',predicted_direction=None)
    with ThreadPoolExecutor(max_workers=6) as pool:refs=list(pool.map(write,range(20)))
    paths = {ref['audit_log'] for ref in refs if ref['audit_log'] != 'stderr'}
    rows = [json.loads(line) for path in paths
            for line in (tmp_path/'logs'/path).read_text().splitlines()]
    rows.extend(row for line in capsys.readouterr().err.splitlines()
                if (row := json.loads(line)).get('event') == 'prediction_result'
                and 'outcome' in row)
    assert {r['run_id'] for r in rows}==set(range(20))
    assert len({r['audit_id'] for r in rows})==20
    assert all(r['predicted_direction'] is None for r in rows)


def test_audit_lock_contention_preserves_full_evidence_without_waiting(tmp_path, monkeypatch):
    """独立进程持锁时，审计调用及时返回完整降级证据。"""
    import fcntl
    import os
    import subprocess
    import sys
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setenv('BFL_RUNTIME_ROOT', str(tmp_path))
    log = tmp_path/'logs/prediction-input-quality'/f'{datetime.now(ZoneInfo("Asia/Shanghai")).date()}.jsonl'
    log.parent.mkdir(parents=True)
    command = (
        'import json; from shared.prediction_audit import emit_prediction_audit; '
        'print(json.dumps(emit_prediction_audit("prediction_result", '
        'scheme_id="fixture", predict_date="2026-11-08", '
        'status="failed", outcome="not_computable", predicted_direction=None)))'
    )
    with log.open('a') as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        completed = subprocess.run(
            [sys.executable, '-B', '-c', command],
            env=dict(os.environ), capture_output=True, text=True, timeout=3,
            check=True,
        )
    reference = json.loads(completed.stdout)
    events = [json.loads(line) for line in completed.stderr.splitlines()]
    event = next(row for row in events if row['event'] == 'prediction_result')
    assert reference['audit_log'] == 'stderr'
    assert event['audit_id'] == reference['audit_id']
    assert event['outcome'] == 'not_computable'
    assert event['predicted_direction'] is None
    assert log.read_text() == ''


def test_native_fallback_stderr_relay_is_scoped_and_redacted(capsys):
    from shared.prediction_audit import relay_native_audit_stderr
    event={'event':'prediction_input_quality','audit_id':'a'*32,'scheme_id':'m',
           'predict_date':'2026-10-29','report':{'status':'incomplete','findings':[{'column':'factor','dates':['2026-10-28']}]}}
    wrong={**event,'scheme_id':'other'}
    stderr='DATABASE_URL=secret password=secret\n'+json.dumps(wrong)+'\n'+json.dumps(event)+'\n'
    relay_native_audit_stderr(stderr,scheme_id='m',predict_date='2026-10-29')
    output=capsys.readouterr().err
    assert 'secret' not in output and 'other' not in output
    assert json.loads(output)['report']['findings'][0]['column']=='factor'


def test_catalog_covers_each_deployment_and_execution_file():
    from scheduler.discovery import load_scheme_config,blackbox_deliveries
    root=Path(__file__).resolve().parents[1]
    matrix=json.loads((root/'deploy/scheme_deployment_matrix_v1.json').read_text())['schemes']
    policy=json.loads((root/'deploy/prediction_input_quality_v1.json').read_text())
    assert set(matrix)<=set(policy['schemes'])
    for sid in matrix:
        cfg=load_scheme_config(root/'schemes'/sid/'config.yaml')
        rule=policy['schemes'][sid]
        assert rule['scheme_version']==cfg.scheme_version
        scripts=[d.script_path for d in blackbox_deliveries(cfg)] if cfg.runtime_type=='blackbox_v2' else [cfg.path/'predict.py']
        assert {p.name for p in scripts}==set(rule['scripts'])
        for script in scripts:
            binding=rule['scripts'][script.name]
            assert binding['sha256']==hashlib.sha256(script.read_bytes()).hexdigest()
            assert binding['profiles']
            for name in binding['profiles']:
                assert policy['profiles'][name]['requirements']
                assert all(r['mode']=='audit' for r in policy['profiles'][name]['requirements'])
                assert all(r['filename'] in {'daily_output.csv','weekly_output.csv','monthly_output.csv'} for r in policy['profiles'][name]['requirements'])
