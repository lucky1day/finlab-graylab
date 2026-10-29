"""封存输入的显式质量策略，不改变算法或exact身份。"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from shared.prediction_input_quality import InputDataIncomplete, check_prediction_input_quality


def setup_case(tmp_path):
    script=tmp_path/'model.py';script.write_text('# immutable delivery\n')
    data=tmp_path/'data';data.mkdir()
    dates=pd.bdate_range('2026-09-01',periods=25).strftime('%Y-%m-%d')
    frame=pd.DataFrame({'date':dates,'required':range(25),'optional':[None]*25})
    frame.to_csv(data/'daily_output.csv',index=False)
    rule={'scheme_version':'exact1','script_sha256':hashlib.sha256(script.read_bytes()).hexdigest(),'requirements':[{'filename':'daily_output.csv','columns':['required'],'offset':1,'rows':11,'min_valid':11,'mode':'block'}]}
    policy=tmp_path/'policy.json';policy.write_text(json.dumps({'schema_version':'prediction-input-quality-v1','schemes':{'model':rule}}))
    return SimpleNamespace(scheme_id='model',scheme_version='exact1'),script,data,SimpleNamespace(feature_date=dates[-1],daily_cutoff_key=dates[-1]),policy,frame


def test_missing_required_window_blocks_but_zero_and_optional_nan_are_valid(tmp_path):
    cfg,script,data,request,policy,frame=setup_case(tmp_path)
    assert check_prediction_input_quality(cfg,script,data,request,policy_path=policy)['status']=='complete'
    frame.loc[23,'required']=None;frame.to_csv(data/'daily_output.csv',index=False)
    with pytest.raises(InputDataIncomplete,match='INPUT_INCOMPLETE.*required'):
        check_prediction_input_quality(cfg,script,data,request,policy_path=policy)


def test_exact_and_script_identity_mismatch_are_blocked(tmp_path):
    cfg,script,data,request,policy,frame=setup_case(tmp_path)
    cfg.scheme_version='different'
    with pytest.raises(InputDataIncomplete,match='POLICY_MISMATCH'):
        check_prediction_input_quality(cfg,script,data,request,policy_path=policy)
    cfg.scheme_version='exact1';script.write_text('# changed\n')
    with pytest.raises(InputDataIncomplete,match='POLICY_MISMATCH'):
        check_prediction_input_quality(cfg,script,data,request,policy_path=policy)


def test_cutoff_is_exact_and_future_rows_do_not_repair_missing_history(tmp_path):
    cfg,script,data,request,policy,frame=setup_case(tmp_path)
    frame.loc[23,'required']=None
    frame.loc[len(frame)]=['2026-10-30',999,None];frame.to_csv(data/'daily_output.csv',index=False)
    with pytest.raises(InputDataIncomplete,match='INPUT_INCOMPLETE'):
        check_prediction_input_quality(cfg,script,data,request,policy_path=policy)
    request.daily_cutoff_key='2026-12-31'
    with pytest.raises(InputDataIncomplete,match='INPUT_INCOMPLETE'):
        check_prediction_input_quality(cfg,script,data,request,policy_path=policy)


def test_audit_policy_and_unknown_coverage_are_explicit(tmp_path):
    cfg,script,data,request,policy,frame=setup_case(tmp_path)
    obj=json.loads(policy.read_text());obj['schemes']['model']['requirements'][0]['mode']='audit';policy.write_text(json.dumps(obj))
    frame.loc[23,'required']=None;frame.to_csv(data/'daily_output.csv',index=False)
    assert check_prediction_input_quality(cfg,script,data,request,policy_path=policy)['status']=='audit_incomplete'
    cfg.scheme_id='unregistered'
    assert check_prediction_input_quality(cfg,script,data,request,policy_path=policy)['status']=='not_declared'


def test_enough_values_but_too_few_valid_differences_is_incomplete(tmp_path):
    cfg,script,data,request,policy,frame=setup_case(tmp_path)
    obj=json.loads(policy.read_text());rule=obj['schemes']['model']['requirements'][0]
    rule.update(offset=0,rows=20,min_valid=10,min_valid_differences=9)
    policy.write_text(json.dumps(obj))
    frame.loc[frame.index%2==0,'required']=None
    frame.to_csv(data/'daily_output.csv',index=False)
    with pytest.raises(InputDataIncomplete,match='valid_differences'):
        check_prediction_input_quality(cfg,script,data,request,policy_path=policy)
