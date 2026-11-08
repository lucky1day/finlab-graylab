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

SEED = 20261105

TARGETS = {'5Y': 'TB5YWI0C'}

SCHEME_ID = 'yield_5y_t5_f977ab2a92ce072366c8'

TENOR = '5Y'

FROZEN_CONFIG = {'family': 'fixed', 'delay': 2, 'threshold': 0.5, 'orientation': -1, 'feature': 'D:S5470409|ema20'}

FROZEN_FEATURES = ['D:S5470409|ema20']

# 用户授权：仅对指定原始因子，在截止前的有序输入上前向填充。
FORWARD_FILL_FACTORS = ('S5470409',)

EMBEDDED_DICTIONARY = [{'indicators_code': 'TB5YWI0C', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}, {'indicators_code': 'S5470409', 'frequency': 'daily', 'source_frequency': '日', 'lag_length': 0}]

FACTOR_VERSIONS = {'TB5YWI0C': 'V1.0', 'S5470409': 'V1.0'}

TENOR_CODES = {1: 'TB1YWI0C', 3: 'TB3YWI0C', 5: 'TB5YWI0C', 7: 'TB7YWI0C', 10: 'TB0YWI0C'}
RAW_LAGS = {'TB5YWI0C': 0, 'S5470409': 0}
def transform(frame: pd.DataFrame, recipes: list[str], native: bool=False) -> dict[str,pd.DataFrame]:
    result={};change=frame.diff()
    scale=change.rolling(13 if native else 60,min_periods=8 if native else 40).std(ddof=0).replace(0,np.nan)
    for recipe in recipes:
        if recipe.startswith("z"):
            k=int(recipe[1:]);r=frame.rolling(k,min_periods=k);out=(frame-r.mean())/r.std(ddof=0).replace(0,np.nan)
        elif recipe.startswith("diff"):
            k=int(recipe[4:]);out=frame.diff(k)/(scale*np.sqrt(k))
        elif recipe.startswith("ema"):
            k=int(recipe[3:]);out=(frame-frame.ewm(span=k,min_periods=k,adjust=False).mean())/scale
        elif recipe.startswith("rank"):
            k=int(recipe[4:]);out=frame.rolling(k,min_periods=k).rank(pct=True)*2-1
        elif recipe.startswith("rsi"):
            k=int(recipe[3:]);up=change.clip(lower=0).ewm(alpha=1/k,min_periods=k,adjust=False).mean();down=(-change.clip(upper=0)).ewm(alpha=1/k,min_periods=k,adjust=False).mean()
            out=(up-down)/(up+down).replace(0,np.nan)
        elif recipe.startswith("vol"):
            k=int(recipe[3:]);out=change.rolling(k,min_periods=k).std(ddof=0)/scale-1
        elif recipe=="autocorr60":out=change.rolling(60,min_periods=60).corr(change.shift(1))
        else:raise ValueError(recipe)
        result[recipe]=out.replace([np.inf,-np.inf],np.nan).clip(-8,8).astype(np.float32)
    return result


def periodic_release(native: np.ndarray,release: np.ndarray,n: int) -> np.ndarray:
    # Hold a completed period's value until the next release, including its NaN.
    # Column ffill would silently bridge an invalid period and is forbidden.
    lookup=np.searchsorted(release,np.arange(n),side="right")-1
    out=np.full((n,native.shape[1]),np.nan,dtype=np.float32)
    valid=lookup>=0;out[valid]=native[lookup[valid]]
    return out


@dataclass
class Inputs:
    dates: pd.DatetimeIndex
    target_dates: pd.DatetimeIndex
    raw: pd.DataFrame
    x: pd.DataFrame
    y: dict[str, np.ndarray]
    delta: dict[str, np.ndarray]
    audit: dict

def features(raw: pd.DataFrame, data_dir: Path) -> pd.DataFrame:
    kind, name = FROZEN_CONFIG['feature'].split(':', 1)
    source, recipe = name.split('|')
    if kind == 'D':
        if source.startswith('curve_'):
            hi, lo = map(int, source.split('_')[1:])
            value = raw[TENOR_CODES[hi]] - raw[TENOR_CODES[lo]]
        elif source.startswith('curvature_'):
            lo, mid, hi = map(int, source.split('_')[1:])
            weight = (mid-lo)/(hi-lo)
            value = raw[TENOR_CODES[mid]] - ((1-weight)*raw[TENOR_CODES[lo]] + weight*raw[TENOR_CODES[hi]])
        else:
            signal = raw[source]
            if source in FORWARD_FILL_FACTORS:
                filled = signal.ffill()
                LOG.info('input_forward_fill factor=%s frequency=daily filled=%d leading_unfilled=%d',
                         source, int((signal.isna() & filled.notna()).sum()), int(filled.isna().sum()))
                signal = filled
            value = signal if source.startswith('TB') or RAW_LAGS[source] >= 1 else signal.shift(1)
        native = pd.DataFrame({source: value}, index=raw.index)
        result = transform(native, [recipe])[recipe].to_numpy(np.float32)
    elif kind == 'W':
        calendar = pd.read_csv(data_dir/'api_wind_date.csv', dtype='string', keep_default_na=False)
        calendar['rdate'] = pd.to_datetime(calendar.rdate)
        keys = calendar.set_index('rdate').week_id.reindex(raw.index)
        if keys.isna().any():
            raise ValueError('Missing authoritative feature week map')
        keys = keys.to_numpy(str)
        transitions = np.flatnonzero(keys[1:] != keys[:-1])+1
        release = transitions+1
        keep = release < len(keys)
        release = release[keep]
        completed = [keys[i-1] for i in transitions[keep]]
        allowed = set(completed)
        path = data_dir/'weekly_output.csv'
        header = csv_header(path)
        if header[0] != 'week_id' or source not in header:
            raise ValueError('Missing frozen weekly source')
        catalog = pd.read_csv(data_dir/'factor_catalog.csv', dtype='string', keep_default_na=False)
        if catalog.loc[catalog.frequency.eq('weekly'), 'indicators_code'].tolist() != header[1:]:
            raise ValueError('Weekly catalog order differs from columns')
        values = {}
        seen = set()
        position = header.index(source)
        with path.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.reader(handle)
            next(reader)
            for row in reader:
                key = row[0].strip()
                if not re.fullmatch(r'\d{6}', key) or key in seen:
                    raise ValueError('Malformed or duplicate weekly key')
                seen.add(key)
                if key not in allowed:
                    continue
                if len(row) != len(header):
                    raise ValueError('Malformed weekly row')
                text = row[position].strip()
                value = np.nan if text.lower() in ('','nan','none','null','na') else float(text)
                if np.isinf(value):
                    raise ValueError('Infinite weekly input')
                values[key] = value
        native = pd.DataFrame({source: [values.get(key, np.nan) for key in completed]}, index=completed)
        transformed = transform(native, [recipe], native=True)[recipe].to_numpy(np.float32)
        result = periodic_release(transformed, release, len(raw))
    else:
        raise ValueError('Unsupported frozen source family')
    return pd.DataFrame(result, index=raw.index, columns=FROZEN_FEATURES, dtype='float32')

class Bank:
    def __init__(self,x,delta,current,target_valid):
        self.x=np.asarray(x,dtype=np.float32);self.delta=delta;self.current=current;self.end=np.maximum(current-5,0);self.target_valid=target_valid
        self.sign=np.nan_to_num(np.sign(x),nan=0).astype(np.int8);self.truth=np.nan_to_num(np.sign(delta),nan=0)[:,None]
        self.valid=np.isfinite(delta)[:,None]&np.isfinite(x)&(self.sign!=0)
        self.score=self.sign*self.truth*self.valid
        self.cn=self.cum(self.valid);self.cy=self.cum(self.score);self.cache={}
    def cum(self,a):return np.vstack([np.zeros((1,self.x.shape[1])),np.cumsum(a,axis=0,dtype=np.float64)])
    def rolling(self,cum,window):return cum[self.end]-cum[np.maximum(self.end-window,0)]
    def predict(self,c):
        family=c['family'];at=self.current;sig=self.sign[at];present=np.isfinite(self.x[at])&self.target_valid[at,None]
        if family=='fixed':
            loc=at-int(c.get('delay',0));safe=np.maximum(loc,0);xx=self.x[safe]
            result=np.where(np.abs(xx)>c['threshold'],self.sign[safe]*c['orientation'],0)
            present = self.target_valid[at,None] & (loc>=0)[:,None] & np.isfinite(xx)
            self.internal_score=xx*c['orientation']
            return np.where(present,result,-2).astype(np.int8).T
        if family=='rolling':count=self.rolling(self.cn,c['window']);edge=self.rolling(self.cy,c['window'])/np.maximum(count,1);direction=np.sign(edge)*sig
        elif family=='exponential':
            half=c['half_life'];key=('exponential',half)
            if key not in self.cache:
                a=0.5**(1/half)
                self.cache[key]=(np.vstack([np.zeros((1,self.x.shape[1])),lfilter([1.],[1.,-a],self.score,axis=0)]),np.vstack([np.zeros((1,self.x.shape[1])),lfilter([1.],[1.,-a],self.valid,axis=0)]))
            cy,cn=self.cache[key];count=self.cn[self.end];edge=cy[self.end]/np.maximum(cn[self.end],1e-12);direction=np.sign(edge)*sig
        elif family in ('conditional','bucket'):
            groups=(-1,1) if family=='conditional' else (0,1,2,3)
            states=self.sign if family=='conditional' else np.where(self.x<-1,0,np.where(self.x<0,1,np.where(self.x<1,2,3)))
            current_states=states[at];count=np.zeros(sig.shape);score=np.zeros(sig.shape)
            for group in groups:
                key=(family,group)
                if key not in self.cache:
                    valid=self.valid&(states==group);self.cache[key]=(self.cum(valid*self.truth),self.cum(valid))
                cy,cn=self.cache[key];chosen=current_states==group
                count=np.where(chosen,self.rolling(cn,c['window']),count);score=np.where(chosen,self.rolling(cy,c['window']),score)
            edge=score/np.maximum(count,1);direction=np.sign(edge)
        elif family=='ridge':
            if 'ridge' not in self.cache:
                ok=np.isfinite(self.x)&np.isfinite(self.delta)[:,None];x=np.where(ok,self.x,0).astype(float);y=np.where(ok,self.delta[:,None],0)
                self.cache['ridge']=[self.cum(a) for a in (ok,x,y,x*x,x*y,y*y)]
            count,sx,sy,sxx,sxy,syy=[self.rolling(a,c['window']) for a in self.cache['ridge']]
            den=np.maximum(count,1);var=np.maximum(sxx-sx*sx/den,0);cov=sxy-sx*sy/den
            slope=cov/(var+1.);pred=sy/den+slope*(self.x[at]-sx/den)
            resid=np.maximum((syy-sy*sy/den-2*slope*cov+slope*slope*var)/den,1e-12)
            edge=pred/np.sqrt(resid);direction=np.sign(edge);present &= var>1e-10
        else:raise ValueError(family)
        zero=sig==0 if family!='ridge' else np.zeros(sig.shape,dtype=bool)
        result=np.where((np.abs(edge)>c['threshold'])&~zero,direction,0)
        present &= (count>=c['minimum'])|zero
        self.internal_score=edge
        return np.where(present,result,-2).astype(np.int8).T



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
        if row['daily_cutoff_key'] != row['feature_date']:
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
        if code not in catalog_index.index or catalog_index.loc[code, 'frequency'] != item['frequency'] or catalog_index.loc[code, 'factor_version'] != FACTOR_VERSIONS[code]:
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
    raw = pd.read_csv(path, nrows=count, usecols=['date', 'TB5YWI0C', 'S5470409'], float_precision='round_trip')
    dates = pd.DatetimeIndex(pd.to_datetime(raw.pop('date'), errors='raise'))
    if (dates.dayofweek >= 5).any():
        raise ValueError('Daily input must use actual trading observations')
    raw.index = dates
    raw = raw.apply(pd.to_numeric, errors='raise')
    if len(raw) != count or dates[-1].strftime('%Y-%m-%d') != cutoff or np.isinf(raw.to_numpy()).any():
        raise ValueError('Invalid numeric history or cutoff')
    for code in TARGETS.values():
        if code not in raw:
            raise ValueError('Invalid target history')
    x = features(raw, data_dir)
    if any(name not in x for name in FROZEN_FEATURES):
        raise ValueError('Insufficient frozen feature history')
    x = x[FROZEN_FEATURES]
    target_dates = pd.DatetimeIndex(pd.Series(dates).shift(-5))
    y: dict[str, np.ndarray] = {}
    delta: dict[str, np.ndarray] = {}
    for tenor, code in TARGETS.items():
        delta[tenor] = raw[code].shift(-5).to_numpy() - raw[code].to_numpy()
        y[tenor] = np.sign(delta[tenor]).astype('float32')
    return Inputs(dates, target_dates, raw, x, y, delta, {})


def forecast(a: Inputs, indices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if (indices < 252).any():
        raise ValueError('Insufficient historical input rows')
    delta = a.delta[TENOR]
    valid = np.isfinite(a.raw[TARGETS[TENOR]].to_numpy(float))
    bank = Bank(a.x.to_numpy(np.float32), delta, indices, valid)
    direction = bank.predict(FROZEN_CONFIG)[0]
    if not np.isin(direction, [-1, 0, 1]).all():
        raise ValueError('Required feature or mature calibration history is unavailable')
    score = bank.internal_score[:, 0]
    if not np.isfinite(score).all():
        raise ValueError('Nonfinite internal score')
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
    # This candidate has no additional calendar-date override.
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

