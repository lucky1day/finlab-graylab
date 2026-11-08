"""W4 固定版本的运行输入审计；仍用本机legacy DB及唯一input_artifacts入口。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Iterator
import uuid

from shared.prediction_audit import emit_prediction_audit


@dataclass
class NativeInputAudit:
    """一次运行可关联到事实的审计引用。"""
    summary: dict[str, Any]


@contextmanager
def native_prediction_input_audit(cfg: Any, predict_date: str) -> Iterator[NativeInputAudit]:
    """先审计再执行；审计异常记日志，模型自身不可计算由原执行器记录。"""
    from shared.calendar_service import get_calendar
    from shared.data_contract import capture_source_commit_evidence_from_connection
    from shared.input_artifacts import (build_daily_input_artifact, build_weekly_input_artifact,
                                       build_monthly_input_artifact, create_input_engine)
    from shared.prediction_context import (build_daily_live_context, build_weekly_live_context,
                                          build_monthly_live_context)
    from shared.prediction_input_quality import audit_prediction_input_quality
    from shared.source_runtime_database import load_source_runtime_database_config

    engine = None
    before = None
    feature_date = None
    result = NativeInputAudit({'status': 'check_error', 'enforcement': 'log_only'})
    try:
        try:
            config = load_source_runtime_database_config()
            engine = create_input_engine(database_config=config)
            calendar = get_calendar(engine)
            if cfg.frequency == 'daily':
                context = build_daily_live_context(calendar, predict_date, horizon=cfg.horizon)
            elif cfg.frequency == 'weekly':
                context = build_weekly_live_context(calendar, predict_date)
            else:
                context = build_monthly_live_context(calendar, predict_date)
            feature_date = context.feature_date
            week_key = str(getattr(context, 'feature_week_id', calendar.week_id_for_date(feature_date)))
            month_key = feature_date[:7].replace('-', '')
            request = SimpleNamespace(predict_date=predict_date, feature_date=feature_date,
                                      daily_cutoff_key=feature_date, weekly_cutoff_key=week_key,
                                      monthly_cutoff_key=month_key)
            frames = {}
            # 独立审计view避免与源adapter不同参数的同名view冲突；不复用旧审计缓存。
            view_id = 'input-audit-' + cfg.scheme_id + '-' + uuid.uuid4().hex[:8]
            with TemporaryDirectory(prefix='native-input-audit-') as temporary:
                with engine.connect().execution_options(isolation_level='REPEATABLE READ') as conn:
                    with conn.begin():
                        before = capture_source_commit_evidence_from_connection(conn, feature_date=feature_date)
                        daily = build_daily_input_artifact(scheme_id=view_id, predict_date=predict_date,
                                    start_date='2010-01-01', end_date=feature_date, engine=conn, output_root=temporary)
                        frames['daily_output.csv'] = daily.dataframe
                        if cfg.frequency in ('daily', 'weekly'):
                            weekly = build_weekly_input_artifact(scheme_id=view_id, predict_date=predict_date,
                                        end_week=int(week_key), as_of_date=feature_date, engine=conn, output_root=temporary)
                            frames['weekly_output.csv'] = weekly.dataframe
                        if cfg.frequency in ('daily', 'monthly'):
                            monthly = build_monthly_input_artifact(scheme_id=view_id, predict_date=predict_date,
                                        start_date='2010-01-01', end_date=feature_date, engine=conn, output_root=temporary)
                            frames['monthly_output.csv'] = monthly.dataframe
                result.summary = audit_prediction_input_quality(cfg, cfg.path/'predict.py', None, request,
                                    frames=frames, calendar=calendar, snapshot_id=before.source_commit_token,
                                    target_tenor=cfg.tenors[0])
                result.summary['source'] = 'local_legacy_db_via_input_artifacts'
        except Exception as exc:  # 审计不能改变固定算法的执行资格。
            ref = emit_prediction_audit('prediction_input_quality', scheme_id=cfg.scheme_id,
                                       predict_date=predict_date, status='check_error',
                                       error_type=type(exc).__name__, enforcement='log_only',
                                       coverage='native_exported_dataset', platform_imputation='none')
            result.summary = {**ref, 'status': 'check_error', 'enforcement': 'log_only'}
        try:
            yield result
        finally:
            if engine is not None:
                try:
                    if before is not None:
                        with engine.connect() as conn:
                            after = capture_source_commit_evidence_from_connection(conn, feature_date=feature_date)
                        changed = before.source_commit_token != after.source_commit_token
                        emit_prediction_audit('native_input_audit_source_observation', scheme_id=cfg.scheme_id,
                                              predict_date=predict_date, status='source_changed' if changed else 'stable',
                                              evidence_type='source_watermark_not_full_value_digest',
                                              before_token=before.source_commit_token, after_token=after.source_commit_token,
                                              enforcement='log_only')
                        result.summary['source_changed_during_audit'] = changed
                except Exception as exc:
                    emit_prediction_audit('native_input_audit_source_observation', scheme_id=cfg.scheme_id,
                                          predict_date=predict_date, status='check_error', error_type=type(exc).__name__)
    finally:
        if engine is not None:
            engine.dispose()

