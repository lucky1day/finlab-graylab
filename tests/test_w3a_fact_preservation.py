"""W3A 八条固定历史来源的隔离合同；不依赖 outputs 或调用算法。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from unittest.mock import patch

import pytest
from sqlalchemy import text

from scheduler import repository as repo
from test_same_id_runtime_upgrade import migration, _sqlite_upsert
from test_same_id_runtime_upgrade_reclaim import reclaim_scope
from test_same_id_runtime_upgrade_mysql import mysql_migration, MYSQL_URL
from test_same_id_live_preservation import preservation_apply, preservation_plan


def _insert(conn, table, row):
    return conn.execute(text(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join(':'+key for key in row)})"), row)


def w3a_fixture(fixture):
    engine, reclaim, _ = reclaim_scope(fixture, 'W3A')
    sources, targets = reclaim['source_configs'], reclaim['new_configs']
    evidence = {'schema_version': 'same-id-live-preservation-control-plane-v1', 'wave': 'W3A',
                'deployment_target': 'aliyun-gray',
                'scheduler': {'control_plane': 'systemd_one_shot', 'timer_fenced': True, 'unique_writer': True},
                'identity_conversions': {}, 'source_inputs': {}}
    with engine.begin() as conn:
        row = lambda table, where='1=1': dict(conn.execute(text(f'SELECT * FROM {table} WHERE {where} LIMIT 1')).mappings().one())
        backtest, detail = row('t_backtest_runs'), row('t_backtest_predictions')
        for definition in ('scheme_version VARCHAR(64)', 'runtime_type VARCHAR(32)', 'run_type VARCHAR(32)',
                           'prediction_phase VARCHAR(32)', 'data_snapshot_id VARCHAR(128)', 'input_artifact_id VARCHAR(128)',
                           'started_at DATETIME', 'finished_at DATETIME', 'records_expected INT', 'records_returned INT', 'records_written INT'):
            conn.exec_driver_sql('ALTER TABLE t_scheme_runs ADD ' + definition)
        for key, spec in repo._W3A_PRESERVATION.items():
            source, target = sources[key], targets[key]
            source.scheme_version, target.scheme_version = spec['source_version'], spec['target_version']
            full = key.startswith('liwei_0616_5y01')
            if full:
                source.code_hash = 'ad9bdacf5063a427ecc8b70852e045f4822ba9af1b6d8fcd171cd2d779e95103'
                target.code_hash = '0fa034ca6fcd9ad358895ccd4c6617a43191cffaff1176823f8dd0ad7b8cbc39'
                source.manifest_hash = '96d46ee4b2fb16f3b7da0c5485808f52f8f7ef14607721fbe00d26bc55d74982'
                target.manifest_hash = 'ae7bfee67c9eae88c10b57cb32901c7ad206febfebde5435f5ba3c4346c6da69'
            conn.execute(text("UPDATE t_scheme_versions SET status='retired' WHERE scheme_id IN (:id,:alias) AND status='active'"),
                         {'id': key, 'alias': source.scheme_id})
            upsert = _sqlite_upsert if engine.dialect.name == 'sqlite' else repo._upsert_scheme_version_conn
            for cfg, status in ((source, 'retired'), (target, 'active')):
                upsert(conn, cfg, trusted_status=status, approved_by='fixture', approved_at=datetime(2026, 9, 12))
                conn.execute(text('UPDATE t_scheme_registry SET runtime_type=:runtime,status=:status WHERE base_scheme_id=:id'),
                             {'runtime': 'blackbox_v2', 'status': 'archived' if cfg is source else 'active', 'id': cfg.scheme_id})
            conversion = {'schema_version': 'native-runtime-identity-conversion-v1', 'source_scheme_id': source.scheme_id,
                'scheme_id': key, 'source_code_sha256': source.code_hash, 'candidate_code_sha256': target.code_hash,
                'source_metadata_sha256': source.manifest_hash, 'candidate_metadata_sha256': target.manifest_hash,
                'algorithm_executions': 0}
            if full:
                conversion.update(schema_version='w3a-weekly-revision-delivery-conversion-v1',
                    permitted_algorithm_change='weekly_raw_prefix_to_feature_suffix_invalidation',
                    weekly_length_guard_preserved=True, requires_independent_suffix_evidence=True)
                evidence['reviewed_revision'] = {'schema_version': 'reviewed-w3a-weekly-revision-source-v1',
                    'scheme_id': key, 'scheme_version': target.scheme_version, 'algorithm_executions': 0,
                    'publication_authorized': False, 'artifacts_sha256': {
                        '/opt/bond-factor-lab/incoming/w3a-full-weekly-independent-20260912-938529/complete.json':
                        '6c37299e795612bff2d8b76745fe9d2dc981cb38db6e4041403f7a3c8b58f813'}}
            evidence['identity_conversions'][key] = conversion
            snapshot = 'snapshot-old-backtest'
            summary = {'scheme_version': source.scheme_version, 'manifest_hash': source.manifest_hash,
                       'runtime_profile': source.runtime_profile, 'row_count': 333,
                       'generation_id': 'generation-old-backtest', 'data_snapshot_id': snapshot}
            _insert(conn, 't_backtest_runs', backtest | {'id': spec['backtest_id'], 'scheme_id': source.scheme_id,
                'benchmark_id': 'preserve-' + key, 'code_hash': source.code_hash, 'config_hash': source.config_hash,
                'summary': json.dumps(summary)})
            for i, target_date in enumerate(('2025-02-14', '2025-05-07', '2026-09-16', '2026-09-17')):
                predict = ('2025-02-07', '2025-04-25', '2026-09-10', '2026-09-11')[i]
                feature = predict if i < 2 else ('2026-09-09' if i == 2 else '2026-09-10')
                snapshot = 'snapshot-old-backtest' if i < 2 else 'snapshot-' + predict
                generation = 'generation-old-backtest' if i < 2 else 'generation-' + predict
                evidence['source_inputs'][snapshot] = {'snapshot_id': snapshot, 'generation_id': generation,
                    'manifest_sha256': 'a' * 64, 'files': {name: 'b' * 64 for name in
                        ('daily_output.csv', 'weekly_output.csv', 'monthly_output.csv', 'factor_catalog.csv', 'api_wind_date.csv')}}
                if i < 2:
                    _insert(conn, 't_backtest_predictions', detail | {'id': spec['detail_ids'][i], 'run_id': spec['backtest_id'],
                        'scheme_id': source.scheme_id, 'target_tenor': '5Y', 'horizon': 5, 'predict_date': predict,
                        'feature_date': feature, 'target_date': target_date, 'predicted_direction': 1, 'label': 1 if i == 0 else -1})
                else:
                    _insert(conn, 't_scheme_runs', {'run_id': spec['run_ids'][i-2], 'scheme_id': source.scheme_id,
                        'scheme_version': source.scheme_version, 'runtime_type': 'blackbox_v2', 'run_type': 'active',
                        'prediction_phase': 'scheduled_live', 'predict_date': predict, 'status': 'success',
                        'data_snapshot_id': snapshot, 'started_at': predict+' 07:03:00', 'finished_at': predict+' 07:03:01',
                        'records_expected': 1, 'records_returned': 1, 'records_written': 1})
                _insert(conn, 't_scheme_predictions', {'id': spec['prediction_ids'][i], 'scheme_id': source.scheme_id,
                    'scheme_version': source.scheme_version, 'target_tenor': '5Y', 'horizon': 5,
                    'predict_date': predict, 'feature_date': feature, 'target_date': target_date,
                    'predicted_direction': 1 if i < 2 else -1, 'run_id': spec['run_ids'][i-2] if i >= 2 else None,
                    'backtest_run_id': spec['backtest_id'] if i < 2 else None,
                    'backtest_actual_direction': (1 if i == 0 else -1) if i < 2 else None,
                    'extra': json.dumps({'request_id': ':'.join((source.scheme_id,predict,feature,target_date)),
                        'data_snapshot_id': snapshot, 'generation_id': generation, 'data_generation_id': generation})})
    evidence['source_canonical_selection'] = {key: repo._same_id_reclaim_identity(cfg) for key,cfg in sources.items()}
    evidence['target_canonical_selection'] = {key: repo._same_id_reclaim_identity(cfg) for key,cfg in targets.items()}
    kwargs = {'wave': 'W3A', 'source_configs': sources, 'target_configs': targets,
              'expected_database_name': reclaim['expected_database_name'], 'expected_server_uuid': reclaim['expected_server_uuid']}
    if engine.dialect.name == 'mysql':
        with engine.begin() as conn:
            missing = conn.execute(text('SELECT DISTINCT p.run_id FROM t_scheme_predictions p LEFT JOIN t_scheme_runs r ON p.run_id=r.run_id WHERE p.run_id IS NOT NULL AND r.run_id IS NULL')).scalars().all()
            for run_id in missing:
                _insert(conn,'t_scheme_runs',{'run_id':run_id,'scheme_id':'unrelated','predict_date':'2026-01-01','status':'success'})
            missing_backtests = conn.execute(text('SELECT DISTINCT p.backtest_run_id FROM t_scheme_predictions p LEFT JOIN t_backtest_runs r ON p.backtest_run_id=r.id WHERE p.backtest_run_id IS NOT NULL AND r.id IS NULL')).scalars().all()
            for run_id in missing_backtests:
                _insert(conn,'t_backtest_runs',backtest | {'id':run_id,'scheme_id':'unrelated','benchmark_id':'unrelated-parent-'+str(run_id)})
            conn.exec_driver_sql('ALTER TABLE t_scheme_predictions ADD CONSTRAINT w3a_run_fk FOREIGN KEY(run_id) REFERENCES t_scheme_runs(run_id) ON DELETE RESTRICT')
            conn.exec_driver_sql('ALTER TABLE t_scheme_predictions ADD CONSTRAINT w3a_backtest_fk FOREIGN KEY(backtest_run_id) REFERENCES t_backtest_runs(id) ON DELETE RESTRICT')
    all_ids = sorted([*sources, *(cfg.scheme_id for cfg in sources.values())])
    with engine.connect() as conn:
        selected = []
        for spec in repo._W3A_PRESERVATION.values():
            for index, source_id in enumerate(spec['prediction_ids']):
                p = dict(conn.execute(text('SELECT * FROM t_scheme_predictions WHERE id=:id'), {'id': source_id}).mappings().one())
                if index < 2:
                    selected.append({'prediction': p,
                        'backtest_run': dict(conn.execute(text('SELECT * FROM t_backtest_runs WHERE id=:id'), {'id':spec['backtest_id']}).mappings().one()),
                        'backtest_prediction': dict(conn.execute(text('SELECT * FROM t_backtest_predictions WHERE id=:id'), {'id':spec['detail_ids'][index]}).mappings().one())})
                else:
                    selected.append({'prediction':p,'run':dict(conn.execute(text('SELECT * FROM t_scheme_runs WHERE run_id=:id'), {'id':p['run_id']}).mappings().one())})
        params = {f'id_{i}':value for i,value in enumerate(all_ids)}
        sqlscope = ','.join(':'+key for key in params)
        existing = {'facts':repo._same_id_fact_snapshot_conn(conn,all_ids,for_update=False),
            'versions':repo._same_id_rows_conn(conn,'t_scheme_versions',f'scheme_id IN ({sqlscope})',params,order='scheme_id,scheme_version',for_update=False),
            'registry':repo._same_id_rows_conn(conn,'t_scheme_registry',f'base_scheme_id IN ({sqlscope})',params,order='scheme_id',for_update=False)}
        identity = (dict(conn.execute(text('SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid')).mappings().one())
                    if engine.dialect.name=='mysql' else {'isolated_test':'sqlite'})
    kwargs['backup_evidence'] = {'schema_version':'same-id-live-preservation-backup-v1','backup_uri':'/private/W3A.json',
        'backup_sha256':'c'*64,'restore_receipt_sha256':'d'*64,'restoration_verified':True,
        'source_database_identity_sha256':repo.native_successor_plan_sha256(identity),
        'source_rows_sha256':repo.native_successor_plan_sha256({'sources':selected}),
        'existing_facts_sha256':repo.native_successor_plan_sha256(existing)}
    return engine, kwargs, evidence


@pytest.fixture
def w3a(migration):
    return w3a_fixture(migration)


def check_preserved(fixture):
    engine, kwargs, control = fixture
    plan = preservation_plan(engine,kwargs,control)
    result = preservation_apply(engine,kwargs,control,plan)
    assert result['records_written']==8 and result['manual_runs_created']==4
    with engine.connect() as conn:
        for item in result['imports']:
            p=dict(conn.execute(text('SELECT * FROM t_scheme_predictions WHERE id=:id'),{'id':item['prediction_id']}).mappings().one())
            assert (p['run_id'] is None)!=(p['backtest_run_id'] is None)
            assert p['scheme_version']==kwargs['target_configs'][p['scheme_id']].scheme_version
            extra=json.loads(p['extra'])['migration_import']
            assert extra['operation']=='history-materialization' and extra['algorithm_executions']==0
            if item['run_id'] is None:
                assert p['backtest_run_id'] in (278,279) and p['backtest_actual_direction'] in (-1,1)
                assert extra['shared_source_evidence_retained'] is True
            else:
                run=conn.execute(text('SELECT run_type,prediction_phase FROM t_scheme_runs WHERE run_id=:id'),{'id':item['run_id']}).one()
                assert tuple(run)==('manual',None)
    with pytest.raises(RuntimeError,match='existing target key'):
        preservation_plan(engine,kwargs,control)


def test_eight_sources_keep_backtest_xor_actuals_and_old_history(w3a):
    check_preserved(w3a)


@pytest.mark.parametrize('number',[4,8])
def test_partial_insert_failure_rolls_back_all_eight_and_four_runs(w3a,monkeypatch,number):
    engine,kwargs,control=w3a
    plan=preservation_plan(engine,kwargs,control)
    original=repo._insert_run_predictions_conn
    calls=[]
    def insert(conn,rows):
        calls.append(True)
        if len(calls)==number:
            raise RuntimeError('injected insert failure')
        return original(conn,rows)
    monkeypatch.setattr(repo,'_insert_run_predictions_conn',insert)
    with pytest.raises(RuntimeError,match='injected'):
        preservation_apply(engine,kwargs,control,plan)
    assert preservation_plan(engine,kwargs,control)==plan


@pytest.mark.parametrize('drift',['parent','detail','actual','xor','source_id','version','input','backup_db','revision','full_code','existing'])
def test_invalid_source_or_partial_existing_key_rejected(w3a,drift):
    engine,kwargs,control=w3a
    with engine.begin() as conn:
        if drift=='parent': conn.execute(text("UPDATE t_backtest_runs SET status='failed' WHERE id=278"))
        elif drift=='detail': conn.execute(text('UPDATE t_backtest_predictions SET predicted_direction=-1 WHERE id=91144'))
        elif drift=='actual': conn.execute(text('UPDATE t_scheme_predictions SET backtest_actual_direction=0 WHERE id=39936'))
        elif drift=='xor': conn.execute(text('UPDATE t_scheme_predictions SET run_id=5605 WHERE id=39936'))
        elif drift=='source_id': conn.execute(text('UPDATE t_scheme_predictions SET id=90000 WHERE id=39936'))
        elif drift=='version': conn.execute(text("UPDATE t_scheme_predictions SET scheme_version='wrong' WHERE id=39936"))
        elif drift=='input': control['source_inputs']['snapshot-old-backtest']['generation_id']='wrong'
        elif drift=='backup_db': kwargs['backup_evidence']['source_database_identity_sha256']='0'*64
        elif drift=='revision': control['reviewed_revision']['publication_authorized']=True
        elif drift=='full_code': next(iter(kwargs['target_configs'].values())).code_hash='0'*64
        else:
            source=dict(conn.execute(text('SELECT * FROM t_scheme_predictions WHERE id=39936')).mappings().one())
            source.pop('id'); source['scheme_id']=next(iter(repo._W3A_PRESERVATION))
            _insert(conn,'t_scheme_predictions',source)
    with pytest.raises((ValueError,RuntimeError)):
        preservation_plan(engine,kwargs,control)


@pytest.mark.skipif(not MYSQL_URL,reason='requires explicit isolated MySQL URL')
def test_mysql_eight_sources_and_shared_backtests(mysql_migration):
    check_preserved(w3a_fixture(mysql_migration))


@pytest.mark.skipif(not MYSQL_URL,reason='requires explicit isolated MySQL URL')
def test_mysql_eighth_insert_failure_rolls_back_runs_products_and_sources(mysql_migration):
    from sqlalchemy.exc import OperationalError
    engine,kwargs,control=w3a_fixture(mysql_migration)
    before=preservation_plan(engine,kwargs,control)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE t_scheme_predictions ADD CONSTRAINT stop_eighth_w3a CHECK (scheme_id <> 'liwei_0616_cons_sda_k3_div_k10' OR target_date <> '2026-09-17')")
    with pytest.raises(OperationalError):
        preservation_apply(engine,kwargs,control,before)
    assert preservation_plan(engine,kwargs,control)==before


@pytest.mark.parametrize('table,column,row_id',[('t_backtest_runs','summary',278),('t_backtest_predictions','extra',91144)])
def test_shared_backtest_evidence_cannot_change_inside_transaction(w3a,monkeypatch,table,column,row_id):
    engine,kwargs,control=w3a
    before=preservation_plan(engine,kwargs,control)
    original=repo._insert_run_predictions_conn
    def insert(conn,rows):
        result=original(conn,rows)
        conn.execute(text(f'UPDATE {table} SET {column}=:value WHERE id=:id'),{'value':'{"changed":true}','id':row_id})
        return result
    monkeypatch.setattr(repo,'_insert_run_predictions_conn',insert)
    with pytest.raises(RuntimeError,match='preexisting facts'):
        preservation_apply(engine,kwargs,control,before)
    assert preservation_plan(engine,kwargs,control)==before
