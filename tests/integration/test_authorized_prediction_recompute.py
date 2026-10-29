"""授权当日替换：备份围栏、跨批事实/run原子回滚及旧历史保留。"""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from scheduler import repository as repo
from shared.models import PredictionRecord

pytestmark = pytest.mark.mysql_integration


def test_today_replacement_is_atomic(mysql_test_engine, monkeypatch):
    day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    configs = [SimpleNamespace(scheme_id=sid, scheme_version='repair_exact',
               path=Path('/unused') / sid, runtime_type='native_adapter',
               horizon=5, tenors=('5Y',)) for sid in ('repair_a', 'repair_b')]
    monkeypatch.setattr(repo, 'load_scheme_config', lambda path: next(c for c in configs if c.scheme_id == path.parent.name))
    # 此合同专测事务；active/Registry审批由既有独立合同覆盖。
    monkeypatch.setattr(repo, '_validate_gray_gap_active_version', lambda *args: None)
    monkeypatch.setattr(repo, '_validate_gray_gap_active_registry', lambda *args: None)
    with mysql_test_engine.begin() as conn:
        for cfg in configs:
            run_id = conn.execute(text("INSERT INTO t_scheme_runs (scheme_id,predict_date,status) VALUES (:sid,:day,'success')"), {'sid': cfg.scheme_id, 'day': day}).lastrowid
            conn.execute(text("INSERT INTO t_scheme_predictions (run_id,scheme_id,scheme_version,target_tenor,horizon,predict_date,feature_date,target_date,predicted_direction) VALUES (:run_id,:sid,'repair_exact','5Y',5,:day,:day,'2099-01-01',0)"), {'sid': cfg.scheme_id, 'day': day, 'run_id': run_id})
    def read():
        with mysql_test_engine.connect() as conn:
            return [dict(r) for r in conn.execute(text('SELECT * FROM t_scheme_predictions WHERE predict_date=:day ORDER BY id'), {'day': day}).mappings()]
    old = read()
    items = [{'cfg': cfg, 'records': [PredictionRecord(cfg.scheme_id, '5Y', 5, day, '2099-01-01', -1, feature_date=day, prediction_phase='gray_live', scheme_version=cfg.scheme_version)], 'duration_sec': 1} for cfg in configs]
    kwargs = dict(predict_date=day, expected_backup_digest=repo.prediction_day_backup_digest(old), expected_count=len(old), authorization='replace-todays-published-predictions', items=items)
    with pytest.raises(RuntimeError, match='changed since backup'):
        repo.replace_authorized_prediction_day(mysql_test_engine, **{**kwargs, 'expected_backup_digest': '0'*64})
    with pytest.raises(ValueError, match='authorization'):
        repo.replace_authorized_prediction_day(mysql_test_engine, **{**kwargs, 'predict_date': '2000-01-01'})
    insert = repo._insert_run_predictions_conn
    def fail_second(conn, rows):
        if rows[0]['scheme_id'] == 'repair_b':
            raise RuntimeError('simulated failure after first insert')
        return insert(conn, rows)
    monkeypatch.setattr(repo, '_insert_run_predictions_conn', fail_second)
    with pytest.raises(RuntimeError, match='simulated failure'):
        repo.replace_authorized_prediction_day(mysql_test_engine, **kwargs)
    assert read() == old
    with mysql_test_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM t_scheme_runs WHERE scheme_id IN ('repair_a','repair_b')")).scalar_one() == 2
    monkeypatch.setattr(repo, '_insert_run_predictions_conn', insert)
    run_ids = repo.replace_authorized_prediction_day(mysql_test_engine, **kwargs)
    assert set(run_ids) == {'repair_a', 'repair_b'}
    assert all(row['predicted_direction'] == -1 for row in read())
