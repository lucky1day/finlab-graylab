"""无法计算的每日结果保留run/日志，方向为null且绝不插入虚构预测。"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import text

from scheduler.executor import execute_scheme, _launchd_scheduled_execution_context

pytestmark=pytest.mark.mysql_integration


@pytest.mark.parametrize('preflight',[None,'data_bridge_unavailable','calendar_unavailable'])
def test_not_computable_result_preserves_run_and_empty_direction(mysql_test_engine,tmp_path,monkeypatch,preflight):
    monkeypatch.setenv('BFL_RUNTIME_ROOT',str(tmp_path/'runtime'))
    cfg=SimpleNamespace(scheme_id='audit_outcome_'+str(preflight or 'model'),scheme_version='audit_exact',
                        runtime_type='native_adapter',status='active',frequency='daily',
                        tenors=['1Y'],horizon=1,schedule=None)
    with (
        patch('scheduler.executor._verify_scheme_activation',return_value=(True,'')),
        patch('scheduler.executor._active_registry_targets',return_value={('1Y',1)}),
        patch('scheduler.executor.run_configured_scheme',side_effect=ValueError('missing raw observation')) as model,
    ):
        result=execute_scheme(cfg,'2026-01-02',prediction_phase='scheduled_live',
                              scheduled_control_plane='launchd_one_shot',
                              scheduled_execution_context=_launchd_scheduled_execution_context(),
                              canonical_config_trusted=True,engine=mysql_test_engine,
                              scheduled_preflight_failure=preflight)
    assert result.status=='failed' and result.run_id is not None and result.records_written==0
    if preflight:model.assert_not_called()
    else:model.assert_called_once()
    with mysql_test_engine.connect() as conn:
        row=conn.execute(text('SELECT status,records_written,error_message FROM t_scheme_runs WHERE run_id=:id'),{'id':result.run_id}).mappings().one()
        assert row['status']=='failed' and row['records_written']==0 and row['error_message']
        assert conn.execute(text('SELECT COUNT(*) FROM t_scheme_predictions WHERE run_id=:id'),{'id':result.run_id}).scalar_one()==0
        assert conn.execute(text('SELECT COUNT(*) FROM t_scheme_run_log WHERE run_id=:id'),{'id':result.run_id}).scalar_one()==1
    events=[json.loads(line) for p in (tmp_path/'runtime/logs/prediction-input-quality').glob('*.jsonl') for line in p.read_text().splitlines()]
    event=next(e for e in events if e['event']=='prediction_result')
    assert event['run_id']==result.run_id and event['outcome']=='not_computable'
    assert event['directions']==[{'target_tenor':'1Y','horizon':1,'predicted_direction':None}]
