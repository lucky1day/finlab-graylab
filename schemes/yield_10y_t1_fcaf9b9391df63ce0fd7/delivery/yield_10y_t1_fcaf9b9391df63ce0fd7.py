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

TARGETS = {'10Y': 'TB0YWI0C'}

SCHEME_ID = 'yield_10y_t1_fcaf9b9391df63ce0fd7'

TENOR = '10Y'

FROZEN_CONFIG = {'family': 'ordinal_pattern', 'path_window': 10, 'event_threshold': 0.0, 'source': 'D:SI801230', 'window': 126, 'alpha': 5.0, 'threshold': 0.15}



FACTOR_VERSIONS = {'TB0YWI0C': 'V1.0', 'SI801230': 'V1.0'}

TARGET='TB0YWI0C'
HORIZON=1
SOURCE_CODE='SI801230'
SOURCE_EXTRA_LAG=1
CODES=['TB0YWI0C', 'SI801230']

@dataclass
class Inputs:
    dates: pd.DatetimeIndex
    target_dates: pd.DatetimeIndex
    raw: pd.DataFrame
    x: pd.DataFrame
    y: dict[str, np.ndarray]
    delta: dict[str, np.ndarray]
    audit: dict

def standardized(levels: np.ndarray) -> np.ndarray:
    frame=pd.DataFrame(levels);change=frame.diff();scale=change.shift(1).rolling(60,min_periods=40).std(ddof=0).replace(0,np.nan)
    return (change/scale).replace([np.inf,-np.inf],np.nan).clip(-8,8).to_numpy(np.float32)


def own_context(y: np.ndarray) -> tuple[np.ndarray,np.ndarray]:
    change=np.full(len(y),np.nan);change[1:]=np.diff(y);valid=np.isfinite(change)
    sign=np.where(valid,np.nan_to_num(np.sign(change),nan=0)+1,-2).astype(np.int16)
    run=np.zeros(len(y),int);previous=-2;length=0
    for i,value in enumerate(sign):
        if value<0:previous=-2;length=0;continue
        length=length+1 if value==previous else 1;previous=int(value);run[i]=length
    bucket=np.where(run<=1,0,np.where(run==2,1,np.where(run<=5,2,3)))
    clock=np.where(valid,sign*4+bucket,-2).astype(np.int16)
    return sign,clock


def state_features(source: np.ndarray,own: np.ndarray,contexts: tuple,rows: np.ndarray,c: dict) -> tuple[np.ndarray,np.ndarray,int,int]:
    family=c['family'];w=int(c['path_window']);n=len(rows);f=source.shape[1]
    if family in ('frequency_control','clock_control'):
        parent=contexts[1][rows];state=np.broadcast_to(parent[:,None],(n,f)).copy();valid=(state>=0)&np.isfinite(own[rows,None]);return state,valid,12,1
    parent=contexts[0][rows];valid=np.broadcast_to((parent>=0)[:,None]&np.isfinite(own[rows,None]),(n,f)).copy()
    if family=='ordinal_pattern':
        valid&=(rows>=2*w)[:,None]
        a=source[np.maximum(rows-2*w,0)].astype(float);b=source[np.maximum(rows-w,0)].astype(float);d=source[rows].astype(float)
        valid&=np.isfinite(a)&np.isfinite(b)&np.isfinite(d)
        comparison=lambda x,y:np.where(x>y,2,np.where(x<y,0,1))
        child=comparison(b,a)*9+comparison(d,b)*3+comparison(d,a);children=27
    elif family=='range_recovery':
        valid&=(rows>=w-1)[:,None];sx=np.zeros((n,f));sy=np.zeros((n,1));lowx=np.zeros_like(sx);highx=np.zeros_like(sx);lowy=np.zeros_like(sy);highy=np.zeros_like(sy);variation=np.zeros_like(sx)
        for lag in range(w-1,-1,-1):
            js=np.maximum(rows-lag,0);x=source[js].astype(float);y=own[js,None].astype(float);valid&=np.isfinite(x)&np.isfinite(y);x=np.nan_to_num(x,nan=0.);y=np.nan_to_num(y,nan=0.)
            sx+=x;sy+=y;variation+=np.abs(x);lowx=np.minimum(lowx,sx);highx=np.maximum(highx,sx);lowy=np.minimum(lowy,sy);highy=np.maximum(highy,sy)
        position=np.divide(sx-lowx,highx-lowx,out=np.full_like(sx,.5),where=highx>lowx);own_position=np.divide(sy-lowy,highy-lowy,out=np.full_like(sy,.5),where=highy>lowy)
        efficiency=np.divide(np.abs(sx),variation,out=np.zeros_like(sx),where=variation>0)
        parent=np.digitize(own_position[:,0],[1/3,2/3]);child=np.digitize(position,[.2,.4,.6,.8])*3+np.digitize(efficiency,[.2,.6]);children=15
    elif family=='cross_recurrence':
        valid&=(rows>=w-1)[:,None];lead=np.zeros((n,f),int);lag_count=np.zeros_like(lead);sync=np.zeros_like(lead);epsilon=float(c['event_threshold'])
        for lag in range(w):
            js=np.maximum(rows-lag,0);x=source[js].astype(float);y=own[js,None].astype(float);valid&=np.isfinite(x)&np.isfinite(y);sync+=np.abs(x-y)<=epsilon
            if lag<w-1:
                prior=np.maximum(js-1,0);xp=source[prior].astype(float);yp=own[prior,None].astype(float);valid&=np.isfinite(xp)&np.isfinite(yp)
                lead+=np.abs(xp-y)<=epsilon;lag_count+=np.abs(x-yp)<=epsilon
        asymmetry=(lead-lag_count)/(w-1);density=sync/w;child=np.digitize(asymmetry,[-.1,.1])*3+np.digitize(density,[.2,.5]);children=9
    else:raise ValueError(family)
    state=parent[:,None]*children+child
    return state.astype(np.int16),valid,3,children


def statistics(source: np.ndarray,own: np.ndarray,contexts: tuple,delta: np.ndarray,current: np.ndarray,c: dict,windows: list[int]) -> dict[int,tuple]:
    stop=current-1;origin=max(0,int(stop.min())-max(windows));rows=np.arange(origin,int(current.max())+1)
    states,valid,parents,children=state_features(source,own,contexts,rows,c);f=source.shape[1];n=len(rows)
    if n>=65535:raise ValueError('Unexpected calibration span for uint16 counts')
    present=valid[current-origin];classes=np.where(np.isfinite(delta[rows]),np.nan_to_num(np.sign(delta[rows]),nan=0)+1,0).astype(int)
    eligible=valid&np.isfinite(delta[rows,None]);safe=np.where(valid,states,0)
    cumulative=np.zeros((n+1,f,parents*children,3),np.uint16)
    cumulative[(np.arange(n)+1)[:,None],np.arange(f)[None,:],safe,classes[:,None]]=eligible
    np.cumsum(cumulative,axis=0,dtype=np.uint16,out=cumulative)
    parent_cum=cumulative.reshape(n+1,f,parents,children,3).sum(axis=3,dtype=np.uint32);global_cum=parent_cum.sum(axis=2,dtype=np.uint32)
    now=np.maximum(states[current-origin],0);parent_now=now//children;fi=np.arange(f)[None,:];out={}
    for window in windows:
        end=stop-origin;start=np.maximum(stop-window,0)-origin
        global_counts=(global_cum[end]-global_cum[start]).transpose(1,0,2).astype(float)
        parent_counts=(parent_cum[end[:,None],fi,parent_now]-parent_cum[start[:,None],fi,parent_now]).transpose(1,0,2).astype(float)
        local_counts=(cumulative[end[:,None],fi,now]-cumulative[start[:,None],fi,now]).transpose(1,0,2).astype(float)
        out[window]=(local_counts,parent_counts,global_counts,(present&(global_counts.sum(axis=-1).T>=40)).T)
    return out


def probabilities(stats: tuple,alpha: float,family: str) -> tuple[np.ndarray,np.ndarray]:
    local,parent,global_counts,present=stats;prior=(global_counts+1)/(global_counts.sum(axis=-1,keepdims=True)+3)
    if family=='frequency_control':return prior,present
    pp=(parent+alpha*prior)/(parent.sum(axis=-1,keepdims=True)+alpha)
    if family=='clock_control':return pp,present
    return (local+alpha*pp)/(local.sum(axis=-1,keepdims=True)+alpha),present


def classify(probs: np.ndarray,present: np.ndarray,threshold: float) -> np.ndarray:
    order=np.array([1,0,2]);winner=order[np.argmax(probs[...,order],axis=-1)];sorted_probs=np.sort(probs,axis=-1);gap=sorted_probs[...,-1]-sorted_probs[...,-2]
    return np.where(present,np.where(gap>threshold,winner-1,0),-2).astype(np.int8)




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
    path=data_dir/'daily_output.csv';header=csv_header(path)
    if header[0]!='date' or set(CODES)-set(header):raise ValueError('Required daily target/source columns unavailable')
    catalog_path=data_dir/'factor_catalog.csv'
    if csv_header(catalog_path)!=['indicators_code','frequency','factor_version']:raise ValueError('Invalid factor catalog schema')
    catalog=pd.read_csv(catalog_path,dtype='string',keep_default_na=False)
    if catalog.empty or catalog.indicators_code.duplicated().any() or (catalog=='').any().any():raise ValueError('Invalid factor catalog')
    lookup=catalog.set_index('indicators_code')
    if catalog.loc[catalog.frequency.eq('daily'),'indicators_code'].tolist()!=header[1:]:raise ValueError('Catalog order differs from daily columns')
    for code in CODES:
        if code not in lookup.index or lookup.loc[code,'frequency']!='daily' or lookup.loc[code,'factor_version']!=FACTOR_VERSIONS[code]:raise ValueError('Frozen factor version/frequency changed')
    keys=pd.read_csv(path,usecols=['date'],dtype='string',keep_default_na=False)['date']
    if keys.empty or keys.duplicated().any() or not keys.is_monotonic_increasing:raise ValueError('Invalid daily keys')
    for key in keys:strict_date(key)
    matches=np.flatnonzero(keys.to_numpy()==cutoff)
    if len(matches)!=1:raise ValueError('Daily cutoff absent from actual observations')
    count=int(matches[0])+1
    raw=pd.read_csv(path,nrows=count,usecols=['date',*CODES],float_precision='round_trip');dates=pd.DatetimeIndex(pd.to_datetime(raw.pop('date'),errors='raise'));raw.index=dates;raw=raw.apply(pd.to_numeric,errors='raise')
    if len(raw)!=count or (dates.dayofweek>=5).any() or np.isinf(raw.to_numpy()).any():raise ValueError('Invalid consumed numeric history')
    delta={TENOR:raw[TARGET].shift(-1).to_numpy()-raw[TARGET].to_numpy()};truth={TENOR:np.sign(delta[TENOR]).astype('float32')};target_dates=pd.DatetimeIndex(pd.Series(dates).shift(-1))
    return Inputs(dates,target_dates,raw,pd.DataFrame(index=dates),truth,delta,{})


def forecast(a: Inputs, indices: np.ndarray) -> tuple[np.ndarray,np.ndarray]:
    cfg=FROZEN_CONFIG
    if cfg['family'] not in ('ordinal_pattern','range_recovery','cross_recurrence'):raise ValueError('Unsupported frozen symbolic mechanism')
    y=a.raw[TARGET].to_numpy(float);own=standardized(y[:,None])[:,0];contexts=own_context(y)
    raw_source=a.raw[SOURCE_CODE].shift(SOURCE_EXTRA_LAG).to_numpy(float).astype(np.float32)[:,None];x=standardized(raw_source)
    stats=statistics(x,own,contexts,a.delta[TENOR],indices,cfg,[cfg['window']])[cfg['window']];probs,present=probabilities(stats,cfg['alpha'],cfg['family']);direction=classify(probs,present,cfg['threshold'])[0]
    if not np.isfinite(probs).all() or not np.isin(direction,[-1,0,1]).all():raise ValueError('Required input invalid or mature training history insufficient')
    return direction,probs[0]



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
                 rows[position]['request_id'], float(np.ravel(score[position])[-1]), int(direction[position]))
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

