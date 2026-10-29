"""在可信运行快照上执行显式、绑定exact的输入质量检查。"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

POLICY_PATH = Path(__file__).resolve().parents[1] / 'deploy' / 'prediction_input_quality_v1.json'


class InputDataIncomplete(ValueError):
    """输入不足或质量策略身份漂移；算法子进程尚未启动。"""


def check_prediction_input_quality(
    cfg: Any, script_path: Path, data_dir: Path, request: Any, *,
    policy_path: Path | None = None,
) -> dict[str, Any]:
    """仅检查声明的列和窗口；合法0有效，非必需列的空值不阻断。"""
    try:
        policy = json.loads((policy_path or POLICY_PATH).read_text(encoding='utf-8'))
        if set(policy) != {'schema_version', 'schemes'} or policy['schema_version'] != 'prediction-input-quality-v1':
            raise ValueError('invalid policy schema')
        rules = policy['schemes'].get(cfg.scheme_id)
        if rules is None:
            return {'status': 'not_declared', 'scheme_id': cfg.scheme_id}
        if (rules['scheme_version'] != cfg.scheme_version
                or rules['script_sha256'] != hashlib.sha256(Path(script_path).read_bytes()).hexdigest()):
            raise InputDataIncomplete(f'INPUT_QUALITY_POLICY_MISMATCH: {cfg.scheme_id}')
        requirements = rules['requirements']
        if not isinstance(requirements, list) or not requirements:
            raise ValueError('empty requirements')
        findings = []
        frames = {}
        for rule in requirements:
            if rule['filename'] != 'daily_output.csv':
                raise ValueError('only explicit daily windows are supported')
            columns = rule['columns']
            if (not isinstance(columns, list) or not columns or len(columns) != len(set(columns))
                    or not all(isinstance(c, str) and c and c != 'date' for c in columns)):
                raise ValueError('invalid required columns')
            offset, rows, minimum = rule['offset'], rule['rows'], rule['min_valid']
            minimum_differences = rule.get('min_valid_differences', 0)
            if (any(type(n) is not int for n in (offset, rows, minimum))
                    or not 0 <= offset <= 1024 or not 1 <= minimum <= rows <= 1024
                    or type(minimum_differences) is not int or not 0 <= minimum_differences < rows
                    or rule['mode'] not in ('block', 'audit')):
                raise ValueError('invalid quality window')
            cache_key = (rule['filename'], tuple(columns))
            if cache_key not in frames:
                frames[cache_key] = pd.read_csv(
                    Path(data_dir)/rule['filename'], usecols=['date', *columns], dtype='string',
                )
            frame = frames[cache_key]
            axis = frame['date']
            dates = pd.to_datetime(axis, format='%Y-%m-%d', errors='raise')
            if axis.duplicated().any() or not dates.is_monotonic_increasing:
                raise ValueError('input date axis must be unique and ordered')
            cutoff = str(request.daily_cutoff_key)
            matches = frame.index[axis.eq(cutoff)].tolist()
            if len(matches) != 1 or cutoff != str(request.feature_date):
                raise InputDataIncomplete(f'INPUT_INCOMPLETE: {cfg.scheme_id} exact daily cutoff absent or mismatched')
            end = matches[0] + 1 - offset
            start = end - rows
            if start < 0:
                raise InputDataIncomplete(f'INPUT_INCOMPLETE: {cfg.scheme_id} insufficient history')
            window = frame.iloc[start:end]
            for column in columns:
                numeric = pd.to_numeric(window[column], errors='coerce')
                valid = numeric.map(lambda v: pd.notna(v) and math.isfinite(float(v)))
                difference_valid = numeric.diff().map(lambda v: pd.notna(v) and math.isfinite(float(v)))
                if int(valid.sum()) < minimum or int(difference_valid.sum()) < minimum_differences:
                    findings.append({'column': column, 'mode': rule['mode'],
                                     'valid': int(valid.sum()), 'required': minimum,
                                     'valid_differences': int(difference_valid.sum()),
                                     'required_differences': minimum_differences,
                                     'dates': window.loc[~valid, 'date'].tolist()})
        blocking = [r for r in findings if r['mode'] == 'block']
        if blocking:
            raise InputDataIncomplete('INPUT_INCOMPLETE: '+cfg.scheme_id+' '+json.dumps(blocking, ensure_ascii=True))
        return {'status': 'audit_incomplete' if findings else 'complete',
                'scheme_id': cfg.scheme_id, 'findings': findings,
                'policy_version': policy['schema_version']}
    except InputDataIncomplete:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise InputDataIncomplete(f'INPUT_QUALITY_POLICY_INVALID: {cfg.scheme_id} {type(exc).__name__}') from exc
