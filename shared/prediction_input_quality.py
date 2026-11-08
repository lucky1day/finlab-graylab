"""检查封存输入并返回详细审计；缺数不阻断预测，不替算法填数。"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

POLICY_PATH = Path(__file__).resolve().parents[1] / 'deploy' / 'prediction_input_quality_v1.json'
_DATASETS = {'daily_output.csv': ('date', 'daily_cutoff_key'),
             'weekly_output.csv': ('week_id', 'weekly_cutoff_key'),
             'monthly_output.csv': ('month_id', 'monthly_cutoff_key')}


class PredictionInputAuditBatch:
    """仅在一次封存 runtime view 内复用读取，不缓存逐日期审计结论。"""

    def __init__(self, data_dir: Path, *, policy_path: Path | None = None) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.policy_path = Path(policy_path or POLICY_PATH).resolve()
        self._policy_loaded = False
        self._policy: Any = None
        self._frames: dict[str, pd.DataFrame | Exception] = {}
        self._script_hashes: dict[Path, str] = {}

    def require_source(
        self, data_dir: Path | None, policy_path: Path | None,
        frames: Mapping[str, pd.DataFrame] | None,
    ) -> None:
        """拒绝把批次读取混用于其他目录、策略或 Native 传入数据。"""
        if (data_dir is None or Path(data_dir).resolve() != self.data_dir
                or frames is not None
                or (policy_path is not None
                    and Path(policy_path).resolve() != self.policy_path)):
            raise ValueError('audit batch source mismatch')

    def read_policy(self) -> Any:
        """读取一次策略；同批读取失败也保持相同的可审计结果。"""
        if not self._policy_loaded:
            try:
                self._policy = json.loads(self.policy_path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                self._policy = exc
            self._policy_loaded = True
        if isinstance(self._policy, Exception):
            raise self._policy
        return self._policy

    def read_frame(self, filename: str) -> pd.DataFrame:
        """读取一次已声明数据集，调用方只读该私有 DataFrame。"""
        if filename not in _DATASETS:
            raise ValueError('unsupported input dataset')
        if filename not in self._frames:
            try:
                self._frames[filename] = pd.read_csv(
                    self.data_dir/filename, dtype='string',
                ).reset_index(drop=True)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._frames[filename] = exc
        frame = self._frames[filename]
        if isinstance(frame, Exception):
            raise frame
        return frame

    def script_sha256(self, path: Path) -> str:
        """同一不可变 release 的执行文件摘要在该批次内复用。"""
        path = Path(path).resolve()
        if path not in self._script_hashes:
            self._script_hashes[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        return self._script_hashes[path]


def _finite(value: object) -> bool:
    return pd.notna(value) and math.isfinite(float(value))


def _axis(frame: pd.DataFrame, name: str) -> pd.Series:
    if name == 'date' and pd.api.types.is_datetime64_any_dtype(frame[name]):
        return frame[name].dt.strftime('%Y-%m-%d').astype('string')
    return frame[name].astype('string')


def check_prediction_input_quality(
    cfg: Any, script_path: Path, data_dir: Path | None, request: Any, *,
    policy_path: Path | None = None, frames: Mapping[str, pd.DataFrame] | None = None,
    calendar: Any = None, batch: PredictionInputAuditBatch | None = None,
) -> dict[str, Any]:
    """缺列、错期、非有限值和历史缺口全部留证；返回的可用旧值不是实际使用值。"""
    result: dict[str, Any] = {
        'status': 'complete', 'scheme_id': cfg.scheme_id,
        'scheme_version': getattr(cfg, 'scheme_version', None), 'enforcement': 'log_only',
        'predict_date': str(request.predict_date) if hasattr(request, 'predict_date') else None,
        'feature_date': str(request.feature_date), 'execution_file': Path(script_path).name, 'findings': [], 'checked_windows': [],
        'platform_imputation': 'none',
        'model_missing_value_handling': 'native_algorithm_unchanged',
        'actual_value_treatment': 'not_exposed_by_delivery',
    }
    findings = result['findings']
    try:
        if batch is not None:
            batch.require_source(data_dir, policy_path, frames)
        policy = (batch.read_policy() if batch is not None else
                  json.loads((policy_path or POLICY_PATH).read_text(encoding='utf-8')))
        if policy.get('schema_version') not in ('prediction-input-quality-v1', 'prediction-input-quality-v2'):
            raise ValueError('invalid policy schema')
        result['policy_version'] = policy['schema_version']
        rules = policy['schemes'].get(cfg.scheme_id)
        if rules is None:
            result.update(status='not_declared', coverage='unverified')
            findings.append({'kind': 'policy_not_declared', 'meaning': 'cannot claim input completeness'})
            return result
        binding = rules.get('scripts', {}).get(Path(script_path).name)
        expected = binding['sha256'] if binding else rules.get('script_sha256')
        if (rules['scheme_version'] != cfg.scheme_version or not expected
                or expected != (batch.script_sha256(script_path) if batch is not None
                                else hashlib.sha256(Path(script_path).read_bytes()).hexdigest())):
            result.update(status='policy_mismatch', coverage='unverified')
            findings.append({'kind': 'policy_identity_mismatch', 'meaning': 'input declaration needs revalidation'})
            return result
        result['coverage'] = rules.get('coverage', 'declared_model_inputs')
        requirements = list(rules.get('requirements', []))
        profile_ids = rules.get('profiles', []) + (binding.get('profiles', []) if binding else [])
        for profile in profile_ids:
            requirements.extend(policy['profiles'][profile]['requirements'])
        if not requirements:
            raise ValueError('empty input declaration')
        cache: dict[str, pd.DataFrame | None] = {}
        for rule in requirements:
            filename = rule['filename']
            if filename not in _DATASETS:
                raise ValueError('unsupported input dataset')
            axis_name, cutoff_name = _DATASETS[filename]
            columns = rule['columns']
            offset, rows, minimum = rule.get('offset', 0), rule['rows'], rule['min_valid']
            differences = rule.get('min_valid_differences', 0)
            if (not columns or len(set(columns)) != len(columns)
                    or any(not isinstance(c, str) or not c or c == axis_name for c in columns)
                    or any(type(n) is not int for n in (offset, rows, minimum, differences))
                    or not 0 <= offset <= 10000 or not 1 <= minimum <= rows <= 10000
                    or not 0 <= differences < rows):
                raise ValueError('invalid input window')
            role = rule.get('role', 'required' if rule.get('mode') == 'block' else 'candidate_or_optional')
            base = {'file': filename, 'role': role, 'offset': offset,
                    'required_rows': rows, 'required_valid': minimum,
                    'required_valid_differences': differences}
            if filename not in cache:
                try:
                    cache[filename] = (batch.read_frame(filename) if batch is not None else
                                       (frames[filename] if frames is not None else
                                        pd.read_csv(Path(data_dir)/filename, dtype='string')).reset_index(drop=True))
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    cache[filename] = None
                    findings.append({**base, 'kind': 'input_unreadable', 'error_type': type(exc).__name__})
            frame = cache[filename]
            if frame is None:
                continue
            if axis_name not in frame:
                findings.append({**base, 'kind': 'missing_axis', 'axis': axis_name})
                continue
            axis = _axis(frame, axis_name)
            if axis.isna().any() or axis.duplicated().any() or not axis.is_monotonic_increasing:
                findings.append({**base, 'kind': 'invalid_axis', 'axis': axis_name})
                continue
            if filename == 'daily_output.csv':
                parsed = pd.to_datetime(axis, format='%Y-%m-%d', errors='coerce')
                valid_axis = parsed.notna().all() and axis.eq(parsed.dt.strftime('%Y-%m-%d')).all()
            else:
                valid_axis = axis.str.fullmatch(r'\d{6}').all()
            if not valid_axis:
                findings.append({**base, 'kind': 'noncanonical_axis'})
                continue
            cutoff = str(getattr(request, cutoff_name))
            base['requested_cutoff'] = cutoff
            hits = frame.index[axis.eq(cutoff)].tolist()
            eligible = frame.index[axis.le(cutoff)].tolist()
            if len(hits) != 1 or (filename == 'daily_output.csv' and cutoff != str(request.feature_date)):
                findings.append({**base, 'kind': 'cutoff_absent_or_mismatched',
                                 'latest_available_axis': str(axis.iloc[eligible[-1]]) if eligible else None})
            end = (eligible[-1]+1 if eligible else 0)-offset
            start = max(0, end-rows)
            window = frame.iloc[start:max(0, end)]
            window_axis = axis.iloc[start:max(0, end)]
            summary = {**base, 'observed_rows': len(window),
                       'observed_start': str(window_axis.iloc[0]) if len(window) else None,
                       'observed_end': str(window_axis.iloc[-1]) if len(window) else None,
                       'columns': columns}
            result['checked_windows'].append(summary)
            if len(window) < rows:
                findings.append({**base, 'kind': 'insufficient_history', 'observed_rows': len(window)})
            if rule.get('calendar_continuity'):
                if calendar is None:
                    findings.append({**base, 'kind': 'calendar_check_unavailable'})
                else:
                    expected = [str(r['rdate']) for r in calendar.period_calendar_rows()
                                if str(r['rdate']) <= cutoff and calendar.is_trading_day(str(r['rdate']))]
                    expected_end = max(0, len(expected)-offset)
                    expected_slice = expected[max(0, expected_end-rows):expected_end]
                    missing_dates = sorted(set(expected_slice)-set(window_axis))
                    if missing_dates:
                        findings.append({**base, 'kind': 'missing_trading_dates', 'dates': missing_dates})
            for column in columns:
                if column not in frame:
                    findings.append({**base, 'column': column, 'kind': 'missing_column',
                                     'last_available_observation': None})
                    continue
                numeric = pd.to_numeric(window[column], errors='coerce')
                valid = numeric.map(_finite)
                valid_diffs = numeric.diff().map(_finite)
                if int(valid.sum()) < minimum or int(valid_diffs.sum()) < differences:
                    history = pd.to_numeric(frame[column].iloc[:max(0, end)], errors='coerce')
                    usable = history.map(_finite)
                    last = history.index[usable].tolist()
                    latest = last[-1] if last else None
                    findings.append({**base, 'column': column, 'kind': 'missing_or_nonfinite_values',
                                     'valid': int(valid.sum()), 'valid_differences': int(valid_diffs.sum()),
                                     'dates': window_axis.loc[~valid].astype(str).tolist(),
                                     'last_available_observation': None if latest is None else
                                     {'snapshot_axis': str(axis.loc[latest]), 'value': float(history.loc[latest])},
                                     'actual_used_observation': 'not_exposed_by_delivery',
                                     'treatment': 'continue_native_model_handling_no_platform_fill'})
        if findings:
            result['status'] = 'incomplete'
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        result['status'] = 'check_error'
        result['coverage'] = 'unverified'
        safe_messages = {'invalid policy schema', 'empty input declaration', 'unsupported input dataset', 'invalid input window'}
        detail = str(exc) if str(exc) in safe_messages else None
        findings.append({'kind': 'quality_check_error', 'error_type': type(exc).__name__, 'detail': detail})
        return result


def audit_prediction_input_quality(cfg: Any, script_path: Path, data_dir: Path | None,
                                   request: Any, *, snapshot_id: str | None = None, target_tenor: str | None = None,
                                   **kwargs: Any) -> dict[str, Any]:
    """将完整核查写入独立JSONL，并返回供预测事实关联的简短引用。"""
    from shared.prediction_audit import emit_prediction_audit
    report = check_prediction_input_quality(cfg, script_path, data_dir, request, **kwargs)
    ref = emit_prediction_audit('prediction_input_quality', scheme_id=cfg.scheme_id,
                               predict_date=str(getattr(request, 'predict_date', request.feature_date)),
                               status=report['status'], target_tenor=target_tenor,
                               data_snapshot_id=snapshot_id, report=report)
    return {**ref, 'status': report['status'], 'finding_count': len(report['findings']),
            'data_snapshot_id': snapshot_id, 'enforcement': 'log_only'}
