"""Mac3 固定十七方案晋级的一次性测试；零算法、仅隔离测试数据库。"""

from dataclasses import replace
from datetime import datetime
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy import MetaData, String, Text
from sqlalchemy.engine import make_url

from scheduler import repository as repo
from scheduler.discovery import load_scheme_config
from shared.prediction_context import WEEKLY_TARGET_RULE
from test_blackbox_activation import _revision_fixture, _sqlite_revision_upsert, _sqlite_datetime_codecs


def _seed(engine):
    old, new = {}, {}
    for key in sorted(repo._ATTACHMENT_RETIREMENT_IDS):
        cfg = load_scheme_config(Path(__file__).resolve().parents[1] / 'schemes' / key / 'config.yaml')
        new[key] = replace(cfg, environment_fingerprint='e' * 64, data_snapshot_id='mac-current-input')
        old[key] = replace(cfg, runtime_type='native_adapter', scheme_version='old-' + hashlib.sha256(key.encode()).hexdigest()[:8],
                           code_hash='a' * 64, config_hash='b' * 64, manifest_hash='c' * 64,
                           target_rule=WEEKLY_TARGET_RULE if cfg.task_type == 'weekly_point' else None)
    with engine.begin() as conn:
        for key, cfg in old.items():
            for version, status in ((cfg, 'active'), (replace(cfg, scheme_version='historical-active'), 'active'),
                                    (replace(cfg, scheme_version='historical-paused'), 'paused')):
                _sqlite_revision_upsert(conn, version, trusted_status=status, approved_by='native-original', approved_at=datetime(2026, 9, 1))
            for tenor in cfg.tenors:
                conn.execute(text("INSERT INTO t_scheme_registry (scheme_id,base_scheme_id,name,description,horizon,task_type,runtime_type,tenors,frequency,target_tenor,schedule_cron,schedule_timezone,status) VALUES (:id,:base,'Original name','Original description',:horizon,:task,'native_adapter',:tenors,:frequency,:tenor,:cron,:tz,'active')"),
                    {'id': f'{key}__h{cfg.horizon}__{tenor}', 'base': key, 'horizon': cfg.horizon, 'task': cfg.task_type,
                     'tenors': json.dumps([tenor]), 'frequency': cfg.frequency, 'tenor': tenor, 'cron': cfg.schedule.cron, 'tz': cfg.schedule.timezone})
            conn.execute(text("INSERT INTO t_scheme_predictions (scheme_id,scheme_version,extra) VALUES (:id,:version,'original-fact')"),
                         {'id': key, 'version': cfg.scheme_version})
        baseline = {'versions': [dict(row) for row in conn.execute(text('SELECT * FROM t_scheme_versions ORDER BY scheme_id,scheme_version')).mappings() if row['scheme_id'] in old],
                    'registry': [dict(row) for row in conn.execute(text('SELECT * FROM t_scheme_registry ORDER BY scheme_id')).mappings() if row['base_scheme_id'] in old]}
    # 实际 controller 经 JSON 保存 baseline，验收日期对象和原字符串摘要一致。
    baseline = json.loads(repo.canonical_native_successor_plan(baseline))
    request = {'request_id': 'one-standard-request', 'predict_date': '2026-09-14', 'feature_date': '2026-09-11',
               'target_date': '2026-09-18', 'daily_cutoff_key': '2026-09-11', 'weekly_cutoff_key': '202637', 'monthly_cutoff_key': '202608'}
    result = {field: request[field] for field in ('request_id', 'predict_date', 'feature_date', 'target_date')} | {'predicted_direction': 1}
    control = {'schema_version': 'mac3-native-promotion-control-v1', 'deployment_target': 'mac3-production',
               'scheme_ids': sorted(old), 'writers_fenced': True, 'state_ready': True, 'standard_results_ready': True,
               'baseline_rows_sha256': repo.native_successor_plan_sha256(baseline),
               'candidate_identities': {key: repo._same_id_reclaim_identity(cfg) for key, cfg in new.items()},
               'states': {key: {'envelope_sha256': 'd' * 64, 'payload_sha256': 'f' * 64} for key, cfg in new.items() if cfg.incremental_state},
               'standard_results': {key: {'scheme_version': cfg.scheme_version, 'environment_fingerprint': cfg.environment_fingerprint,
                   'data_snapshot_id': cfg.data_snapshot_id, 'receipt_sha256': '1' * 64,
                   'results': [{'target_tenor': tenor, 'request': dict(request), 'result': dict(result)} for tenor in cfg.tenors]} for key, cfg in new.items()}}
    return {'old_configs': old, 'new_configs': new, 'baseline_rows': baseline, 'action': 'cutover',
            'expected_database_name': 'isolated', 'expected_server_uuid': 'isolated'}, control


_EXTRA_TABLES = (
    't_scheme_actuals (id INTEGER PRIMARY KEY, actual_direction INTEGER)',
    't_schema_migrations (version INTEGER, state TEXT)',
    't_scheme_runs (run_id INTEGER PRIMARY KEY, scheme_id TEXT, status TEXT)',
    't_harness_runs (harness_run_id TEXT PRIMARY KEY, scheme_id TEXT, scheme_version TEXT, status TEXT)',
    't_harness_gate_results (id INTEGER PRIMARY KEY, harness_run_id TEXT)',
    't_input_artifacts (artifact_id TEXT PRIMARY KEY, scheme_id TEXT)',
    't_backtest_monthly_metrics (id INTEGER PRIMARY KEY, scheme_id TEXT)',
    't_backtest_reproduction_checks (id INTEGER PRIMARY KEY, benchmark_id TEXT)',
)


@pytest.fixture
def promotion(monkeypatch, _sqlite_datetime_codecs):
    engine, _ = _revision_fixture()
    with engine.begin() as conn:
        for ddl in _EXTRA_TABLES:
            conn.exec_driver_sql('CREATE TABLE ' + ddl)
        conn.exec_driver_sql("INSERT INTO t_schema_migrations VALUES (24,'APPLIED')")
        conn.exec_driver_sql('INSERT INTO t_scheme_actuals VALUES (1,1)')
    kwargs, control = _seed(engine)
    monkeypatch.setattr(repo, '_upsert_scheme_version_conn', _sqlite_revision_upsert)
    yield engine, kwargs, control
    engine.dispose()


def _plan(fixture):
    engine, kwargs, control = fixture
    return repo.read_mac3_native_promotion_plan(engine, **kwargs, control_plane_evidence=control)


def _apply(fixture, *, plan=None, reader=None):
    engine, kwargs, control = fixture
    plan = plan or _plan(fixture)
    return repo.apply_mac3_native_promotion(engine, **kwargs, expected_plan_sha256=repo.native_successor_plan_sha256(plan),
        approved_by='test-Mac3-promotion', approved_at=datetime(2026, 9, 12), control_plane_evidence_reader=reader or (lambda: control))


def test_whole_scope_roundtrip_preserves_paused_history_and_unrelated_rows(promotion):
    _, kwargs, _ = promotion
    before = _plan(promotion)
    _apply(promotion); kwargs['action'] = 'rollback'
    after = _plan(promotion)
    assert after['facts'] == before['facts']
    scoped = [row for row in after['versions'] if row['scheme_id'] in kwargs['old_configs']]
    assert len([row for row in scoped if row['status'] == 'active']) == 17
    assert len([row for row in scoped if row['runtime_type'] == 'native_adapter' and row['status'] == 'retired']) == 34
    assert len([row for row in scoped if row['status'] == 'paused']) == 17
    _apply(promotion); kwargs['action'] = 'cutover'
    restored = _plan(promotion)
    assert restored['facts'] == before['facts'] and restored['registry'] == before['registry']
    _apply(promotion); kwargs['action'] = 'rollback'
    assert _plan(promotion)['versions'] == after['versions']


@pytest.mark.parametrize('drift', ['scope', 'host', 'baseline', 'candidate', 'missing_result', 'duplicate_target', 'result_echo', 'direction', 'state', 'snapshot'])
def test_rejects_unapproved_scope_or_evidence(promotion, drift):
    _, kwargs, control = promotion
    key = next(iter(kwargs['new_configs']))
    if drift == 'scope': del kwargs['new_configs'][key]
    elif drift == 'host': control['deployment_target'] = 'aliyun-gray'
    elif drift == 'baseline': control['baseline_rows_sha256'] = '0' * 64
    elif drift == 'candidate': control['candidate_identities'][key]['code_hash'] = 'wrong'
    elif drift == 'missing_result': del control['standard_results'][key]
    elif drift == 'duplicate_target': control['standard_results'][key]['results'] *= 2
    elif drift == 'result_echo': control['standard_results'][key]['results'][0]['result']['request_id'] = 'other'
    elif drift == 'direction': control['standard_results'][key]['results'][0]['result']['predicted_direction'] = 2
    elif drift == 'state': control['states'] = {}
    else: control['standard_results'][key]['data_snapshot_id'] = 'other'
    with pytest.raises((ValueError, RuntimeError)):
        _plan(promotion)


@pytest.mark.parametrize('table,id_field', [('t_scheme_runs', 'run_id'), ('t_backtest_runs', 'id')])
def test_running_work_is_not_blanket_exempt(promotion, table, id_field):
    engine, kwargs, _ = promotion
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO {table} ({id_field},scheme_id,status) VALUES (1,:id,'running')"), {'id': next(iter(kwargs['old_configs']))})
    with pytest.raises(RuntimeError, match='running work'):
        _plan(promotion)


@pytest.mark.parametrize('identity', ['source', 'candidate'])
@pytest.mark.parametrize('action', ['cutover', 'rollback'])
def test_running_harness_for_executable_exact_blocks_transition(promotion, identity, action):
    engine, kwargs, _ = promotion
    if action == 'rollback':
        _apply(promotion)
        kwargs['action'] = action
    cfg = next(iter(kwargs['old_configs' if identity == 'source' else 'new_configs'].values()))
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO t_harness_runs VALUES ('current-work',:id,:version,'running')"),
                     {'id': cfg.scheme_id, 'version': cfg.scheme_version})
    with pytest.raises(RuntimeError, match='running work.*t_harness_runs'):
        _plan(promotion)


def test_historical_harness_running_is_preserved_across_cutover_and_rollback(promotion):
    engine, kwargs, _ = promotion
    key = next(iter(kwargs['old_configs']))
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO t_harness_runs VALUES ('old-audit',:id,'historical-active','running')"), {'id': key})
    before = _plan(promotion)
    _apply(promotion)
    kwargs['action'] = 'rollback'
    assert _plan(promotion)['facts'] == before['facts']
    _apply(promotion)
    kwargs['action'] = 'cutover'
    assert _plan(promotion)['facts'] == before['facts']
    with engine.connect() as conn:
        assert tuple(conn.execute(text('SELECT * FROM t_harness_runs')).one()) == (
            'old-audit', key, 'historical-active', 'running')


@pytest.mark.parametrize('failure', ['last_insert', 'late_control', 'stale_plan', 'actuals'])
def test_atomic_failure_leaves_every_original_row_unchanged(promotion, monkeypatch, failure):
    before = _plan(promotion)
    supplied = dict(before)
    reader = None
    if failure in {'last_insert', 'actuals'}:
        calls = 0
        def fail_last(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 17 and failure == 'last_insert': raise RuntimeError('last insert failed')
            if calls == 1 and failure == 'actuals':
                args[0].exec_driver_sql('UPDATE t_scheme_actuals SET actual_direction=-1')
            return _sqlite_revision_upsert(*args, **kwargs)
        monkeypatch.setattr(repo, '_upsert_scheme_version_conn', fail_last)
    elif failure == 'late_control':
        sequence = iter([promotion[2], promotion[2] | {'changed': True}]); reader = lambda: next(sequence)
    else: supplied['extra'] = 'unapproved'
    with pytest.raises(RuntimeError): _apply(promotion, plan=supplied, reader=reader)
    assert _plan(promotion) == before


def test_unready_preflight_is_read_only_and_never_applies(promotion):
    promotion[2].update(state_ready=False, standard_results_ready=False, states={}, standard_results={})
    prepared = _plan(promotion)
    with pytest.raises(RuntimeError, match='completed local Results'):
        _apply(promotion, plan=prepared)
    assert _plan(promotion) == prepared


@pytest.mark.parametrize('field,value', [('status', 'retired'), ('code_hash', 'other'), ('scheme_version', 'missing')])
def test_source_preimage_changes_block_entire_operation(promotion, field, value):
    engine, kwargs, _ = promotion
    cfg = next(iter(kwargs['old_configs'].values()))
    with engine.begin() as conn:
        conn.execute(text(f'UPDATE t_scheme_versions SET {field}=:value WHERE scheme_id=:id AND scheme_version=:version'),
                     {'value': value, 'id': cfg.scheme_id, 'version': cfg.scheme_version})
    with pytest.raises(RuntimeError): _plan(promotion)


@contextmanager
def _isolated_mysql(source):
    """仅 loopback 随机测试库，finally 删除自己创建的库；无生产连接。"""
    url = make_url(os.environ['BFL_TEST_NATIVE_SUCCESSOR_MYSQL_URL'])
    assert url.host in {'127.0.0.1', 'localhost', '::1'}
    schema = 'bfl_mac3_promotion_' + uuid.uuid4().hex
    admin = create_engine(url.set(database=None))
    created, engine = False, None
    try:
        with admin.begin() as conn:
            assert str(conn.exec_driver_sql('SELECT VERSION()').scalar_one()).startswith('8.')
            conn.exec_driver_sql(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci')
        created = True
        engine = create_engine(url.set(database=schema), connect_args={'init_command': "SET SESSION time_zone = '+00:00'"})
        metadata = MetaData()
        metadata.reflect(bind=source)
        for table in metadata.tables.values():
            for column in table.columns:
                if isinstance(column.type, Text):
                    column.type = String(512 if column.name in {'extra', 'description'} else 128)
        metadata.create_all(engine)
        with source.connect() as source_conn, engine.begin() as conn:
            for table in metadata.sorted_tables:
                rows = [dict(row) for row in source_conn.execute(text('SELECT * FROM ' + table.name)).mappings()]
                if rows: conn.execute(table.insert(), rows)
        yield engine
    finally:
        if engine is not None: engine.dispose()
        if created:
            with admin.begin() as conn:
                conn.exec_driver_sql(f'DROP DATABASE `{schema}`')
                assert conn.execute(text('SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=:name'), {'name': schema}).scalar_one() == 0
        admin.dispose()


@pytest.mark.skipif(not os.getenv('BFL_TEST_NATIVE_SUCCESSOR_MYSQL_URL'), reason='requires isolated loopback MySQL')
@pytest.mark.parametrize('failure', [False, True])
def test_mysql_full_scope_locks_roundtrip_and_late_failure(promotion, monkeypatch, failure):
    source, kwargs, control = promotion
    # 真实 MySQL 回环同时证明历史 running 审计不阻塞且在事务前后完整保留。
    historical_key = next(iter(kwargs['old_configs']))
    with source.begin() as conn:
        conn.execute(text("INSERT INTO t_harness_runs VALUES ('old-audit',:id,'historical-active','running')"),
                     {'id': historical_key})
    monkeypatch.undo()  # MySQL 使用真实仓储 INSERT，不使用 SQLite 方言替身。
    with _isolated_mysql(source) as engine:
        with engine.connect() as conn:
            identity = dict(conn.execute(text('SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid')).mappings().one())
        kwargs = kwargs | {'expected_database_name': identity['database_name'], 'expected_server_uuid': identity['server_uuid']}
        fixture = engine, kwargs, control
        for cfg in (kwargs['old_configs'][historical_key], kwargs['new_configs'][historical_key]):
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO t_harness_runs VALUES ('current-work',:id,:version,'running')"),
                             {'id': cfg.scheme_id, 'version': cfg.scheme_version})
            with pytest.raises(RuntimeError, match='running work.*t_harness_runs'):
                _plan(fixture)
            with engine.begin() as conn:
                conn.exec_driver_sql("DELETE FROM t_harness_runs WHERE harness_run_id='current-work'")
        before = _plan(fixture)
        names = ['bfl:bbv2-draft:' + hashlib.sha256(key.encode()).hexdigest()[:32] for key in kwargs['old_configs']]
        def reader():
            with engine.connect() as conn:
                owners = [conn.execute(text('SELECT IS_USED_LOCK(:name)'), {'name': name}).scalar_one() for name in names]
            assert None not in owners and len(set(owners)) == 1
            return control
        with pytest.raises(RuntimeError, match='database identity mismatch'):
            repo.read_mac3_native_promotion_plan(engine, **(kwargs | {'expected_server_uuid': 'wrong'}), control_plane_evidence=control)
        if failure:
            original = repo._upsert_scheme_version_conn
            count = 0
            def last_failure(*args, **kw):
                nonlocal count
                count += 1
                if count == 17: raise RuntimeError('last MySQL insert failed')
                return original(*args, **kw)
            monkeypatch.setattr(repo, '_upsert_scheme_version_conn', last_failure)
            with pytest.raises(RuntimeError, match='last MySQL'):
                _apply(fixture, reader=reader)
            assert _plan(fixture) == before
        else:
            _apply(fixture, reader=reader); kwargs['action'] = 'rollback'
            assert _plan(fixture)['facts'] == before['facts']
            _apply(fixture, reader=reader); kwargs['action'] = 'cutover'
            assert _plan(fixture)['registry'] == before['registry']
            _apply(fixture, reader=reader)
        with engine.connect() as conn:
            assert all(conn.execute(text('SELECT IS_FREE_LOCK(:name)'), {'name': name}).scalar_one() == 1 for name in names)
