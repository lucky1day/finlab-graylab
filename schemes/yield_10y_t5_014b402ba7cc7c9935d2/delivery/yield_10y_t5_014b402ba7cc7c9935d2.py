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

SCHEME_ID = 'yield_10y_t5_014b402ba7cc7c9935d2'

TENOR = '10Y'

FROZEN_CONFIG = {'balanced': False, 'family': 'multi_model', 'margin': 0.2, 'model': 'logit', 'refit': 5, 'scope': 'risk', 'strength': 0.1, 'topk': 6, 'universe': ['S5111905|z60', 'S5439944|z120', 'S0031553|z60', 'S0029752|z60', 'M0161518|z20', 'G0006353|z60', 'M0143821|z20', 'G0006954|z60', 'G0006352|z60', 'B2559386|z120', 'K7780920|z120', 'S5470395|diff20', 'S5474437|z120', 'S5470395|ema20', 'SSSHF00C|diff60', 'M0161518|diff10', 'S5470325|diff20', 'S5470395|ema60', 'S0142909|z60', 'NISHF01C|diff60', 'S5814964|diff10', 'S5814964|ema20', 'S0142909|z120', 'M0161518|ema20', 'M0161518|diff5', 'S5470395|z60', 'S5470300|diff20', 'M0161518|z60', 'M0143821|z60', 'M1329545|diff60', 'M0143821|ema20', 'S0029756|z60', 'S5470311|diff10', 'S5814929|diff10', 'S5470405|diff20', 'M0161518|ema60', 'S5470395|z20', 'M0143821|diff5', 'S5470311|diff5', 'M0143821|diff10', 'M0143821|ema5', 'S5474439|z20', 'G0003956|z120', 'S6404620|z60', 'S5470333|ema20', '90000020|diff5', 'S5470332|z60', 'M0143821|ema60', 'S5814964|z60', 'SSSHF00C|z60', 'S5814964|diff20', 'S5470311|ema20', 'S5470411|diff20', 'S5470342|diff20', 'H8357500|z120', 'SSSHF00C|ema60', 'S5470325|z60', 'M1329545|ema60', 'S5470325|ema60', 'M0161518|ema5', 'S5474440|z120', 'S5470332|diff20', 'S5470395|diff10', 'S5814964|diff5', 'S0142909|ema60', 'S5470333|diff20', 'J1547601|z120', 'S5814964|ema5', 'S0048261|z120', 'S5470311|ema5', 'F3117461|diff60', 'S5470395|z120', 'S5470311|diff20', 'S0031645|z60', 'S5470333|diff10', 'S5470405|ema20', 'S5474440|z60', 'S5470409|diff20', 'S6404620|diff20', 'S5814929|diff5', 'S5470311|z20', 'SMACDS05|diff20', 'S5470405|diff60', '90000020|ema5', 'S6404620|ema60', 'S5470300|ema20', 'J9760873|diff60', 'S5470395|diff60', 'P2562056|z120', 'S5470405|z20', 'S5470405|z60', 'DR007IB0|ema60', 'M1329545|diff20', 'SFCZC01C|z60', 'S5470325|diff60', 'SMACDS06|z120', 'SWR00007|z120', 'SBW21001|diff10', 'S5814929|ema20', 'S5006004|z120', 'S0142909|diff60', 'SMACDS05|diff60', 'G1235664|z20', 'S0203230|z120', 'S5470395|ema5', 'KDJ010YD|z20', 'SMACDS01|diff60', 'L2573422|diff60', 'KDJ010YD|ema20', 'S6404620|z120', 'SMACDS01|diff20', 'S5814964|diff60', '90000020|ema20', 'S5470332|ema60', 'DR007IB0|z60', 'S5814929|z20', 'SWR00006|z60', 'S5005830|z20', 'S5474440|diff60', 'S0031550|z60'], 'window': 126}

FROZEN_FEATURES = ['S5111905|z60', 'S5439944|z120', 'S0031553|z60', 'S0029752|z60', 'M0161518|z20', 'G0006353|z60', 'M0143821|z20', 'G0006954|z60', 'G0006352|z60', 'B2559386|z120', 'K7780920|z120', 'S5470395|diff20', 'S5474437|z120', 'S5470395|ema20', 'SSSHF00C|diff60', 'M0161518|diff10', 'S5470325|diff20', 'S5470395|ema60', 'S0142909|z60', 'NISHF01C|diff60', 'S5814964|diff10', 'S5814964|ema20', 'S0142909|z120', 'M0161518|ema20', 'M0161518|diff5', 'S5470395|z60', 'S5470300|diff20', 'M0161518|z60', 'M0143821|z60', 'M1329545|diff60', 'M0143821|ema20', 'S0029756|z60', 'S5470311|diff10', 'S5814929|diff10', 'S5470405|diff20', 'M0161518|ema60', 'S5470395|z20', 'M0143821|diff5', 'S5470311|diff5', 'M0143821|diff10', 'M0143821|ema5', 'S5474439|z20', 'G0003956|z120', 'S6404620|z60', 'S5470333|ema20', '90000020|diff5', 'S5470332|z60', 'M0143821|ema60', 'S5814964|z60', 'SSSHF00C|z60', 'S5814964|diff20', 'S5470311|ema20', 'S5470411|diff20', 'S5470342|diff20', 'H8357500|z120', 'SSSHF00C|ema60', 'S5470325|z60', 'M1329545|ema60', 'S5470325|ema60', 'M0161518|ema5', 'S5474440|z120', 'S5470332|diff20', 'S5470395|diff10', 'S5814964|diff5', 'S0142909|ema60', 'S5470333|diff20', 'J1547601|z120', 'S5814964|ema5', 'S0048261|z120', 'S5470311|ema5', 'F3117461|diff60', 'S5470395|z120', 'S5470311|diff20', 'S0031645|z60', 'S5470333|diff10', 'S5470405|ema20', 'S5474440|z60', 'S5470409|diff20', 'S6404620|diff20', 'S5814929|diff5', 'S5470311|z20', 'SMACDS05|diff20', 'S5470405|diff60', '90000020|ema5', 'S6404620|ema60', 'S5470300|ema20', 'J9760873|diff60', 'S5470395|diff60', 'P2562056|z120', 'S5470405|z20', 'S5470405|z60', 'DR007IB0|ema60', 'M1329545|diff20', 'SFCZC01C|z60', 'S5470325|diff60', 'SMACDS06|z120', 'SWR00007|z120', 'SBW21001|diff10', 'S5814929|ema20', 'S5006004|z120', 'S0142909|diff60', 'SMACDS05|diff60', 'G1235664|z20', 'S0203230|z120', 'S5470395|ema5', 'KDJ010YD|z20', 'SMACDS01|diff60', 'L2573422|diff60', 'KDJ010YD|ema20', 'S6404620|z120', 'SMACDS01|diff20', 'S5814964|diff60', '90000020|ema20', 'S5470332|ema60', 'DR007IB0|z60', 'S5814929|z20', 'SWR00006|z60', 'S5005830|z20', 'S5474440|diff60', 'S0031550|z60']

EMBEDDED_DICTIONARY = [{'indicators_code': 'S5111905', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 1}, {'indicators_code': 'S0031553', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 1}, {'indicators_code': 'G0006954', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'K7780920', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 1}, {'indicators_code': 'G0006352', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 2}, {'indicators_code': 'G0006353', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 2}, {'indicators_code': 'S0031550', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 1}, {'indicators_code': 'S6404620', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'SWR00006', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'SWR00007', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'G0003956', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 1}, {'indicators_code': 'B2559386', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 1}, {'indicators_code': 'DR007IB0', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'L2573422', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': '90000020', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'F3117461', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'G1235664', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 2}, {'indicators_code': 'H8357500', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'J1547601', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'J9760873', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'KDJ010YD', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'M0143821', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'M0161518', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'M1329545', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'NISHF01C', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'P2562056', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S0029752', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S0029756', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S0031645', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 1}, {'indicators_code': 'S0048261', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 1}, {'indicators_code': 'S0142909', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S0203230', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 1}, {'indicators_code': 'S5005830', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5006004', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5439944', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 1}, {'indicators_code': 'S5470300', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5470311', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5470325', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5470332', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5470333', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5470342', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5470395', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5470405', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5470409', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5470411', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5474437', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5474439', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5474440', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5814929', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5814964', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'SBW21001', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'SFCZC01C', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'SMACDS01', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'SMACDS05', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'SMACDS06', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'SSSHF00C', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}]

FACTOR_VERSIONS = {'S5111905': 'V1.0', 'S0031553': 'V1.0', 'G0006954': 'V1.0', 'K7780920': 'V1.0', 'G0006352': 'V1.0', 'G0006353': 'V1.0', 'S0031550': 'V1.0', 'S6404620': 'V1.0', 'SWR00006': 'V1.0', 'SWR00007': 'V1.0', 'G0003956': 'V1.0', 'B2559386': 'V1.0', 'DR007IB0': 'V1.0', 'L2573422': 'V1.0', '90000020': 'V1.0', 'F3117461': 'V1.0', 'G1235664': 'V1.0', 'H8357500': 'V1.0', 'J1547601': 'V1.0', 'J9760873': 'V1.0', 'KDJ010YD': 'V1.0', 'M0143821': 'V1.0', 'M0161518': 'V1.0', 'M1329545': 'V1.0', 'NISHF01C': 'V1.0', 'P2562056': 'V1.0', 'S0029752': 'V1.0', 'S0029756': 'V1.0', 'S0031645': 'V1.0', 'S0048261': 'V1.0', 'S0142909': 'V1.0', 'S0203230': 'V1.0', 'S5005830': 'V1.0', 'S5006004': 'V1.0', 'S5439944': 'V1.0', 'S5470300': 'V1.0', 'S5470311': 'V1.0', 'S5470325': 'V1.0', 'S5470332': 'V1.0', 'S5470333': 'V1.0', 'S5470342': 'V1.0', 'S5470395': 'V1.0', 'S5470405': 'V1.0', 'S5470409': 'V1.0', 'S5470411': 'V1.0', 'S5474437': 'V1.0', 'S5474439': 'V1.0', 'S5474440': 'V1.0', 'S5814929': 'V1.0', 'S5814964': 'V1.0', 'SBW21001': 'V1.0', 'SFCZC01C': 'V1.0', 'SMACDS01': 'V1.0', 'SMACDS05': 'V1.0', 'SMACDS06': 'V1.0', 'SSSHF00C': 'V1.0'}

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

from sklearn.linear_model import LogisticRegression, Ridge
from lightgbm import LGBMClassifier

def forecast_block(a: Inputs, tenor: str, cfg: dict, indices: np.ndarray, variants: list[dict] | None=None) -> np.ndarray:
    variants = variants or [{k: cfg[k] for k in ('model', 'strength', 'balanced')}]
    x = a.x[cfg['universe']].to_numpy(float)
    y = a.y[tenor]
    column = TARGETS[tenor]
    scale = a.raw[column].diff().rolling(60, min_periods=40).std(ddof=0).to_numpy() * np.sqrt(5)
    response = np.divide(a.delta[tenor], scale, out=np.full(len(scale), np.nan), where=scale > 1e-09)
    scores = np.full((len(indices), len(variants)), np.nan, dtype='float32')
    fit_rows = indices // cfg['refit'] * cfg['refit']
    for anchor in np.unique(fit_rows):
        eligible = np.flatnonzero((np.arange(len(y)) < anchor - 5) & np.isfinite(y) & np.isfinite(response))[-cfg['window']:]
        if len(eligible) < 60:
            raise ValueError('Insufficient mature training labels')
        positions = np.flatnonzero(fit_rows == anchor)
        train, test = (x[eligible].copy(), x[indices[positions]].copy())
        usable = np.isfinite(train).sum(0) >= max(40, len(train) * 0.8)
        if usable.sum() < 4:
            raise ValueError('Insufficient historical training factors')
        train, test = (train[:, usable], test[:, usable])
        finite_test = np.isfinite(test)
        means = np.nanmean(train, axis=0)
        train = np.where(np.isfinite(train), train, means)
        test = np.where(np.isfinite(test), test, means)
        sd = np.maximum(train.std(0), 1e-06)
        train, test = ((train - means) / sd, (test - means) / sd)
        truth = y[eligible]
        relevance = np.abs((train * (truth - truth.mean())[:, None]).mean(0))
        top = np.argsort(-relevance, kind='stable')[:min(cfg['topk'], train.shape[1])]
        train, test = (train[:, top], test[:, top])
        present = finite_test[:, top].mean(1) >= 0.75
        for j, model_cfg in enumerate(variants):
            model_name = model_cfg['model']
            if model_name == 'ridge':
                model = Ridge(alpha=model_cfg['strength'], solver='svd')
                model.fit(train, response[eligible])
                result = model.predict(test)
            else:
                nonflat = truth != 0
                labels = (truth[nonflat] > 0).astype(int)
                if len(np.unique(labels)) < 2:
                    raise ValueError('Insufficient training label classes')
                if model_name == 'logit':
                    model = LogisticRegression(C=model_cfg['strength'], class_weight='balanced' if model_cfg['balanced'] else None, solver='liblinear', random_state=SEED, max_iter=200)
                else:
                    params = {'n_estimators': 45, 'learning_rate': 0.05, 'num_leaves': int(model_cfg['strength']), 'max_depth': 3, 'min_child_samples': 20, 'reg_lambda': 5.0, 'verbosity': -1, 'random_state': SEED, 'n_jobs': 1, 'deterministic': True, 'force_col_wise': True}
                    model = LGBMClassifier(**params)
                model.fit(train[nonflat], labels)
                probabilities = model.booster_.predict(test) if model_name == 'lgbm' else model.predict_proba(test)[:, 1]
                result = 2 * probabilities - 1
            scores[positions[present], j] = result[present]
    return scores


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

