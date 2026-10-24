"""Frozen selected strategy; 0 means flat/no action under the confirmed business convention."""

from __future__ import annotations

import os
for _thread in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_thread] = "1"

import argparse
import csv
import datetime
import json
import logging
from pathlib import Path
import re
import sys
import tempfile
from dataclasses import dataclass
from itertools import combinations
from typing import Any
import numpy as np
import pandas as pd
from scipy.signal import lfilter

SEED = RANDOM_STATE = 20261019

TARGETS = {'3Y':'TB3YWI0C','5Y':'TB5YWI0C','10Y':'TB0YWI0C'}

SCHEME_ID = 'yield_5y_t5_46279b3a80e7a3bf268d'

TENOR = '5Y'

FROZEN_CONFIG = {'family': 'fixed', 'feature': 'S5470409|diff10', 'orientation': -1}

FROZEN_FEATURES = ['S5470409|diff10']

EMBEDDED_DICTIONARY = [{'indicators_code': 'S5470409', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}]

FACTOR_VERSIONS = {'S5470409': 'V1.0'}

@dataclass
class Inputs:
    dates: pd.DatetimeIndex
    target_dates: pd.DatetimeIndex
    raw: pd.DataFrame
    x: pd.DataFrame
    y: dict[str, np.ndarray]
    delta: dict[str, np.ndarray]
    audit: dict

def features(raw: pd.DataFrame, dictionary: pd.DataFrame) -> pd.DataFrame:
    meta = dictionary[dictionary.frequency.eq('daily')].set_index('indicators_code')
    base: dict[str, pd.Series] = {}
    for col in raw:
        if col not in meta.index or str(meta.loc[col, 'source_frequency']) != '日':
            continue
        lag = int(meta.loc[col, 'lag_length'])
        base[col] = raw[col] if col.startswith('TB') or lag >= 1 else raw[col].shift(1)
    tenors = {1: 'TB1YWI0C', 3: 'TB3YWI0C', 5: 'TB5YWI0C', 7: 'TB7YWI0C', 10: 'TB0YWI0C'}
    for lo, hi in combinations(tenors, 2):
        base[f'curve_{hi}_{lo}'] = raw[tenors[hi]] - raw[tenors[lo]]
    for lo, mid, hi in combinations(tenors, 3):
        w = (mid - lo) / (hi - lo)
        base[f'curvature_{lo}_{mid}_{hi}'] = raw[tenors[mid]] - ((1 - w) * raw[tenors[lo]] + w * raw[tenors[hi]])
    out: dict[str, pd.Series] = {}
    for name, s in base.items():
        for w in (20, 60, 120):
            mean = s.rolling(w, min_periods=w).mean()
            std = s.rolling(w, min_periods=w).std(ddof=0).replace(0, np.nan)
            out[f'{name}|z{w}'] = ((s - mean) / std).clip(-8, 8)
        scale = s.diff().rolling(60, min_periods=40).std(ddof=0).replace(0, np.nan)
        for w in (1, 5, 10, 20, 60):
            out[f'{name}|diff{w}'] = ((s - s.shift(w)) / (scale * np.sqrt(w))).clip(-8, 8)
        for w in (5, 20, 60):
            out[f'{name}|ema{w}'] = ((s - s.ewm(span=w, min_periods=w, adjust=False).mean()) / scale).clip(-8, 8)
    x = pd.DataFrame(out, index=raw.index).replace([np.inf, -np.inf], np.nan).astype('float32')
    old = x.loc[:'2023-12-31']
    usable = (old.notna().sum() >= 252) & (old.std() > 1e-08)
    return x.loc[:, usable]

class Bank:

    def __init__(self, a: Inputs, tenor: str, indices: np.ndarray):
        self.a, self.tenor, self.indices = (a, tenor, indices)
        self.names = a.x.columns.tolist()
        self.signal = np.nan_to_num(np.sign(a.x.to_numpy()), nan=0).astype('int8')
        y = np.nan_to_num(a.y[tenor], nan=0)[:, None]
        self.valid = np.isfinite(a.y[tenor])[:, None] & (self.signal != 0)
        self.score = self.signal * y
        self.cy = np.vstack([np.zeros((1, len(self.names))), np.cumsum(self.score, axis=0, dtype='float32')])
        self.cn = np.vstack([np.zeros((1, len(self.names))), np.cumsum(self.valid, axis=0, dtype='float32')])
        self.upper = np.maximum(indices - 5, 0)
        self.conditional: dict = {}
        self.exponential: dict = {}

    def block(self, cfg: dict) -> np.ndarray:
        sig = self.signal[self.indices]
        family = cfg['family']
        if family == 'fixed':
            self.internal_score = sig * cfg['orientation']
            return self.internal_score.astype('int8')
        end = self.upper
        if family in {'rolling', 'conditional'}:
            start = np.maximum(end - cfg['window'], 0)
            if family == 'rolling':
                count = self.cn[end] - self.cn[start]
                score = self.cy[end] - self.cy[start]
            else:
                if not self.conditional:
                    y = np.nan_to_num(self.a.y[self.tenor], nan=0)[:, None]
                    for sign in (-1, 1):
                        v = (self.signal == sign) & self.valid
                        self.conditional[sign] = (np.vstack([np.zeros((1, len(self.names))), np.cumsum(v * y, axis=0, dtype='float32')]), np.vstack([np.zeros((1, len(self.names))), np.cumsum(v, axis=0, dtype='float32')]))
                count = np.zeros(sig.shape, dtype='float32')
                score = np.zeros(sig.shape, dtype='float32')
                for sign in (-1, 1):
                    cy, cn = self.conditional[sign]
                    current = sig == sign
                    count = np.where(current, cn[end] - cn[start], count)
                    score = np.where(current, cy[end] - cy[start], score)
        elif family == 'exponential':
            half = cfg['half_life']
            if half not in self.exponential:
                self.exponential.clear()
                decay = 0.5 ** (1 / half)
                self.exponential[half] = (np.vstack([np.zeros((1, len(self.names))), lfilter([1.0], [1.0, -decay], self.score, axis=0)]), np.vstack([np.zeros((1, len(self.names))), lfilter([1.0], [1.0, -decay], self.valid, axis=0)]))
            cy, cn = self.exponential[half]
            score, count = (cy[end], self.cn[end])
            edge = score / np.maximum(cn[end], 1e-09)
        else:
            raise ValueError('Unknown family')
        if family != 'exponential':
            edge = score / np.maximum(count, 1)
        self.internal_score, self.internal_count = edge, count
        direction = np.sign(edge) if family == 'conditional' else np.sign(edge) * sig
        return np.where((sig != 0) & (count >= cfg['minimum']) & (np.abs(edge) > cfg['threshold']), direction, 0).astype('int8')


REQUEST_FIELDS = ('request_id', 'predict_date', 'feature_date', 'target_date',
                  'daily_cutoff_key', 'weekly_cutoff_key', 'monthly_cutoff_key')
RESULT_FIELDS = ('request_id', 'predict_date', 'feature_date', 'target_date', 'predicted_direction')
LOG = logging.getLogger('bond_t5_contract')


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def strict_date(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) is None:
        raise ValueError('Date must be YYYY-MM-DD')
    if datetime.date.fromisoformat(value).isoformat() != value:
        raise ValueError('Invalid canonical date')
    return value


def validate_requests(rows: Any) -> list[dict[str, str]]:
    if not isinstance(rows, list) or not rows:
        raise ValueError('Expected nonempty requests')
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(REQUEST_FIELDS):
            raise ValueError('Request fields must match Contract 1.0')
        if any(not isinstance(value, str) or not value for value in row.values()):
            raise ValueError('All request fields must be nonempty strings')
        if not row['request_id'].strip() or row['request_id'] in seen:
            raise ValueError('Empty or duplicate request_id')
        seen.add(row['request_id'])
        for field in ('predict_date', 'feature_date', 'target_date', 'daily_cutoff_key'):
            strict_date(row[field])
        for field in ('weekly_cutoff_key', 'monthly_cutoff_key'):
            if re.fullmatch(r'\d{6}', row[field]) is None:
                raise ValueError('Frequency cutoff keys must be six-digit strings')
        if not row['feature_date'] <= row['predict_date'] <= row['target_date'] or row['feature_date'] >= row['target_date']:
            raise ValueError('Invalid request date ordering')
        if row['daily_cutoff_key'] > row['feature_date']:
            raise ValueError('Daily input cannot be later than feature_date')
    return rows


def csv_header(path: Path) -> list[str]:
    with path.open(encoding='utf-8-sig', newline='') as handle:
        header = next(csv.reader(handle), [])
    if not header or len(header) != len(set(header)):
        raise ValueError('Missing or duplicate CSV columns')
    return header


def load_inputs(data_dir: Path, cutoff: str) -> Inputs:
    path = data_dir / 'daily_output.csv'
    header = csv_header(path)
    if header[0] != 'date':
        raise ValueError('Daily first column must be date')
    catalog_path = data_dir / 'factor_catalog.csv'
    if csv_header(catalog_path) != ['indicators_code', 'frequency', 'factor_version']:
        raise ValueError('Invalid factor_catalog schema')
    catalog = pd.read_csv(catalog_path, dtype='string', keep_default_na=False)
    if catalog.empty or catalog.indicators_code.duplicated().any() or (catalog == '').any().any():
        raise ValueError('Invalid or duplicate factor catalog entries')
    daily_codes = catalog.loc[catalog.frequency.eq('daily'), 'indicators_code'].tolist()
    if daily_codes != header[1:]:
        raise ValueError('Daily catalog order differs from actual columns')
    catalog_index = catalog.set_index('indicators_code')
    for item in EMBEDDED_DICTIONARY:
        code = item['indicators_code']
        if code == '__unused__':
            continue
        if code not in catalog_index.index or catalog_index.loc[code, 'frequency'] != 'daily' or catalog_index.loc[code, 'factor_version'] != FACTOR_VERSIONS[code]:
            raise ValueError('Frozen factor missing or factor version changed')
    keys = pd.read_csv(path, usecols=['date'], dtype='string', keep_default_na=False)['date']
    if keys.empty or keys.duplicated().any() or not keys.is_monotonic_increasing:
        raise ValueError('Daily keys must be unique and chronological')
    for key in keys:
        strict_date(key)
    matches = np.flatnonzero(keys.to_numpy() == cutoff)
    if len(matches) != 1:
        raise ValueError('Daily cutoff key not found exactly once')
    count = int(matches[0]) + 1
    raw = pd.read_csv(path, nrows=count)
    dates = pd.DatetimeIndex(pd.to_datetime(raw.pop('date'), errors='raise'))
    if (dates.dayofweek >= 5).any():
        raise ValueError('Daily input must use actual trading observations')
    raw.index = dates
    raw = raw.apply(pd.to_numeric, errors='raise')
    if len(raw) != count or dates[-1].strftime('%Y-%m-%d') != cutoff or np.isinf(raw.to_numpy()).any():
        raise ValueError('Invalid numeric history or cutoff')
    for code in TARGETS.values():
        if code not in raw or raw.loc['2024-01-01':, code].isna().any() or (raw[code].dropna() <= 0).any():
            raise ValueError('Invalid target history')
    x = features(raw, pd.DataFrame(EMBEDDED_DICTIONARY))
    if any(name not in x for name in FROZEN_FEATURES):
        raise ValueError('Insufficient frozen feature history')
    x = x[FROZEN_FEATURES]
    target_dates = pd.DatetimeIndex(pd.Series(dates).shift(-5))
    y: dict[str, np.ndarray] = {}
    delta: dict[str, np.ndarray] = {}
    for tenor, code in TARGETS.items():
        delta[tenor] = raw[code].shift(-5).to_numpy() - raw[code].to_numpy()
        y[tenor] = np.sign(np.round(delta[tenor], 10)).astype('float32')
    return Inputs(dates, target_dates, raw, x, y, delta, {})


def forecast(a: Inputs, indices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if (indices < 252).any():
        raise ValueError('Insufficient historical input rows')
    cfg = FROZEN_CONFIG
    if cfg['family'] == 'multi_model':
        score = forecast_block(a, TENOR, cfg, indices)[:, 0]
        direction = np.where(np.isfinite(score) & (np.abs(score) > cfg['margin']), np.sign(score), 0).astype('int8')
    else:
        if cfg['family'] == 'local_distribution':
            bank = DistributionBank(a, TENOR, indices)
        else:
            bank = Bank(a, TENOR, indices)
        direction = bank.block(cfg)[:, 0]
        score = bank.internal_score[:, 0]
    if np.isinf(score).any() or not np.isin(direction, [-1, 0, 1]).all() or ((~np.isfinite(score)) & (direction != 0)).any():
        raise ValueError('Cannot generate legal direction')
    return direction, score



def validate_calendar(data_dir: Path, rows: list[dict[str, str]]) -> None:
    path = data_dir / 'api_wind_date.csv'
    if csv_header(path) != ['rdate', 'week_id']:
        raise ValueError('Invalid authoritative calendar schema')
    calendar = pd.read_csv(path, dtype='string', keep_default_na=False)
    if calendar.empty or calendar.rdate.duplicated().any():
        raise ValueError('Invalid authoritative date mapping')
    for date in calendar.rdate:
        strict_date(date)
    if not calendar.week_id.str.fullmatch(r'\d{6}').all():
        raise ValueError('Week keys must be opaque six-digit strings')
    mapping = calendar.set_index('rdate').week_id
    for row in rows:
        if row['daily_cutoff_key'] not in mapping.index or mapping.loc[row['daily_cutoff_key']] != row['weekly_cutoff_key']:
            raise ValueError('Request cutoff does not match authoritative week mapping')


def execution_direction(value: int, row: dict[str, str]) -> int:
    # Preserve the existing conservative execution boundary, including all five
    # January target dates whose feature anchors precede the sim freeze.
    if row['target_date'] >= '2026-01-01' and row['daily_cutoff_key'] <= '2025-12-31':
        return 0
    return int(value)

def calculate(rows: list[dict[str, str]], data_dir: Path) -> list[dict[str, Any]]:
    rows = validate_requests(rows)
    validate_calendar(data_dir, rows)
    a = load_inputs(data_dir, max(row['daily_cutoff_key'] for row in rows))
    indices = a.dates.get_indexer(pd.to_datetime([row['daily_cutoff_key'] for row in rows]))
    if (indices < 0).any():
        raise ValueError('Daily cutoff absent from observed trading axis')
    direction, score = forecast(a, indices)
    samples = sorted({0, len(rows) // 2, len(rows) - 1})
    for position in samples:
        independent = load_inputs(data_dir, rows[position]['daily_cutoff_key'])
        single_direction, single_score = forecast(independent, np.array([len(independent.dates) - 1]))
        same_score = bool(np.array_equal(single_score, score[position:position+1], equal_nan=True))
        if execution_direction(single_direction[0], rows[position]) != execution_direction(direction[position], rows[position]) or not same_score:
            raise ValueError('Independent cutoff score/direction self-check failed')
        LOG.info('batch_self_check request_id=%s score=%.17g direction=%d fallback_used=false',
                 rows[position]['request_id'], float(score[position]), int(direction[position]))
    return [{**{key: row[key] for key in RESULT_FIELDS[:4]}, 'predicted_direction': execution_direction(value, row)}
            for row, value in zip(rows, direction)]


def read_requests(path: Path, single: bool) -> list[dict[str, str]]:
    if single:
        with path.open(encoding='utf-8') as handle:
            value = json.load(handle, object_pairs_hook=unique_object)
        return validate_requests([value])
    if set(csv_header(path)) != set(REQUEST_FIELDS):
        raise ValueError('Batch request header must match seven Contract fields')
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return validate_requests(list(csv.DictReader(handle)))


def atomic_output(path: Path, rows: list[dict[str, Any]], single: bool) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                                         dir=path.parent, prefix=path.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            if single:
                json.dump(rows[0], handle, ensure_ascii=False, allow_nan=False)
                handle.write('\n')
            else:
                writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS, lineterminator='\n')
                writer.writeheader()
                writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description='Frozen T+5 algorithm; 0 means flat/no action')
    sub = parser.add_subparsers(dest='command', required=True)
    for name, flag in (('predict', '--request'), ('backtest', '--requests')):
        command = sub.add_parser(name)
        command.add_argument(flag, type=Path, required=True)
        command.add_argument('--data-dir', type=Path, required=True)
        command.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output
    output_owned = False
    try:
        request_path = args.request if args.command == 'predict' else args.requests
        if output.resolve().is_relative_to(args.data_dir.resolve()) or output.resolve() in {request_path.resolve(), Path(__file__).resolve(), Path(__file__).with_suffix('.json').resolve()}:
            raise ValueError('Output cannot overwrite input or algorithm files')
        output_owned = True
        # Output is platform-owned; remove any stale result before processing.
        if output.exists():
            output.unlink()
        single = args.command == 'predict'
        rows = calculate(read_requests(request_path, single), args.data_dir)
        atomic_output(output, rows, single)
    except Exception as exc:
        if output_owned and output.is_file():
            output.unlink()
        LOG.error('%s: %s', type(exc).__name__, exc)
        sys.exit(2)


if __name__ == '__main__':
    main()

