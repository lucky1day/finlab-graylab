"""Native 平台适配只移交标准方向，原算法审计和输入边界保持独立。"""
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shared import daily_0629_predict_adapter as daily
from shared import monthly_predict_adapter as monthly
from shared import weekly_average_lgbm_predict_adapter as weekly
from shared.signal_policy import no_signal_as_flat


@pytest.mark.parametrize('family', ['daily', 'weekly', 'monthly'])
def test_native_adapters_keep_source_diagnostics_without_platform_confidence(monkeypatch, family):
    """三个存量族保持原日期/方向及概率审计，不依赖平台置信字段。"""
    evidence = SimpleNamespace(frequency='frozen', target_tenor='5Y', target_column='close',
                               source_package_hash='frozen-hash', model_id='model', final_select_id='selected')
    engine = MagicMock()
    calendar = MagicMock()
    calendar.previous_trading_day.return_value = '2026-08-14'
    calendar.nth_trading_day_after.return_value = '2026-08-17'
    calendar.week_id_to_last_trading_day.side_effect = {1: '2026-08-14', 2: '2026-08-21'}.__getitem__
    artifact = SimpleNamespace(path='/frozen/input.json', source='frozen')
    if family == 'daily':
        module, entry, require, runner, builder = (daily, daily.run_daily_0629_prediction,
            'require_daily_0629_source_evidence', 'run_source_daily_live', 'build_daily_input_artifact')
        source = {'frequency': 'frozen', 'pred_label': -1, 'prob_up': 0.81, 'threshold': 0.9,
                  'active_score': 0.75, 'prediction_date': '2026-08-14', 'rdate': '2026-08-17'}
        expected_target = '2026-08-17'
        audit = {'prob_up': 0.81, 'threshold': 0.9, 'active_score': 0.75}
    elif family == 'weekly':
        module, entry, require, runner, builder = (weekly, weekly.run_weekly_average_lgbm_prediction,
            'require_weekly_average_source_evidence', 'run_source_weekly_live', 'build_weekly_input_artifact')
        source = {'frequency': 'frozen', 'pred_label': -1, 'prob_up': 0.81, 'threshold_used': 0.9,
                  'model_margin': -0.09, 'effective_week_id': 1}
        monkeypatch.setattr(module, 'next_calendar_week_id', lambda *_: 2)
        expected_target = '2026-08-21'
        audit = {'prob_up': 0.81, 'threshold_used': 0.9, 'model_margin': -0.09}
    else:
        module, entry, require, runner, builder = (monthly, monthly.run_monthly_prediction,
            'require_monthly_source_evidence', 'run_source_monthly_live', 'build_monthly_input_artifact')
        source = {'frequency': 'frozen', 'y_pred': 0, 'pred_proba_up': 0.81,
                  'pred_proba_down': 0.19, 'validation_overall_accuracy': 0.75,
                  'feature_month_id': '2026-08', 'target_month_id': '2026-09'}
        context = SimpleNamespace(feature_date='2026-08-14', target_date='2026-09-15',
            feature_month_id='2026-08', target_month_id='2026-09', db_rdate='2026-08-15',
            trigger_date='2026-08-15', scheduled_trigger_date='2026-08-15')
        monkeypatch.setattr(module, 'build_monthly_live_context', lambda *_: context)
        expected_target = '2026-09-15'
        audit = {'pred_proba_up': 0.81, 'pred_proba_down': 0.19, 'validation_overall_accuracy': 0.75}
    monkeypatch.setattr(module, require, lambda _: evidence)
    monkeypatch.setattr(module, 'load_source_runtime_database_config', lambda: 'frozen')
    monkeypatch.setattr(module, 'create_input_engine', lambda **_: engine)
    monkeypatch.setattr(module, 'get_calendar', lambda _: calendar)
    monkeypatch.setattr(module, runner, lambda *_, **__: [dict(source)])
    input_builder = MagicMock(return_value=artifact)
    monkeypatch.setattr(module, builder, input_builder)
    output = asdict(entry('frozen_scheme', '2026-08-15')[0])
    assert (output['predict_date'], output['feature_date'], output['target_date']) == (
        '2026-08-15', '2026-08-14', expected_target)
    assert output['predicted_direction'] == -1  # 方向来自源标签，而非平台重新判断概率。
    assert 'confidence' not in output and 'confidence' not in output['extra']
    assert {key: output['extra'][key] for key in audit} == audit
    assert input_builder.call_args.kwargs['predict_date'] == '2026-08-15'
    engine.dispose.assert_called_once_with()


def test_no_signal_policy_keeps_flat_direction_and_immutable_audit():
    """去除平台字段不改变无信号政策或其不可变审计。"""
    source = {'diagnostic': {'confidence_internal': 0.7}}
    outcome = no_signal_as_flat('frozen-component', extra=source)
    source['diagnostic']['confidence_internal'] = 0.2
    assert outcome.predicted_direction == 0
    assert not hasattr(outcome, 'confidence')
    assert outcome.extra['diagnostic']['confidence_internal'] == 0.7
    assert outcome.extra['signal_state'] == 'no_signal'
    with pytest.raises(TypeError):
        outcome.extra['diagnostic']['confidence_internal'] = 0.5
