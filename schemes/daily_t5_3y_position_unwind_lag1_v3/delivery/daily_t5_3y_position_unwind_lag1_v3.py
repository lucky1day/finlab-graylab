"""Frozen position-unwind rule. Contract1.0 CLI; disclosed original abstention exception.

The target is the direct direction of TB3YWI0C at the platform-supplied T+5 date.
Zero preserves original no-action semantics, including the five historical transition
requests. It is not an invalid-input fallback or a newly optimized flat forecast.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date
import json
import logging
import os
from pathlib import Path
import re
import sys

for _thread_key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[_thread_key]='1'

import numpy as np
import pandas as pd

SEED=RANDOM_STATE=20261020
SCHEME_ID='daily_t5_3y_position_unwind_lag1_v3'
ALGORITHM_VERSION='20261112.sopv3.2'
REQUEST_FIELDS=('request_id','predict_date','feature_date','target_date','daily_cutoff_key','weekly_cutoff_key','monthly_cutoff_key')
RESULT_FIELDS=('request_id','predict_date','feature_date','target_date','predicted_direction')
CASH=('TB5YWI00','TB5YWI0H','TB5YWI0L','TB5YWI0C','TB5YWI0V')
FUTURE=('TCFE000K','TCFE000H','TCFE000L','TCFE000C','TCFE000V')
FIELDS=(*CASH,*FUTURE,'TCFE000I')
CONFIG={'lag':1,'window':10,'threshold':0.0,'volatility_window':60,'volatility_min_periods':40,
        'volume_window':126,'volume_min_periods':60,'standard_deviation_ddof':0,
        'intraday_clip':5.0,'volume_clip_low':0.05,'volume_clip_high':5.0,'pressure_clip':6.0}
LOG=logging.getLogger(SCHEME_ID)

def require(condition:bool,message:str)->None:
    if not condition:raise ValueError(message)

def canonical_date(value:str)->str:
    require(isinstance(value,str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',value) is not None,'noncanonical date')
    require(date.fromisoformat(value).isoformat()==value,'invalid date')
    return value

def header(path:Path,expected:tuple[str,...]|None=None)->list[str]:
    require(path.is_file() and path.stat().st_size>0,'missing or empty input file')
    with path.open('r',encoding='utf-8-sig',newline='') as f:
        columns=next(csv.reader(f),[])
    require(bool(columns) and len(columns)==len(set(columns)) and all(columns),'empty or duplicate header')
    if expected is not None:require(columns==list(expected),'unexpected header')
    return columns

def unique_object(pairs:list[tuple[str,object]])->dict:
    result={}
    for key,value in pairs:
        require(key not in result,'duplicate JSON field')
        result[key]=value
    return result

def validate_request(raw:dict)->dict[str,str]:
    require(isinstance(raw,dict) and set(raw)==set(REQUEST_FIELDS),'Request must contain exactly seven fields')
    require(all(isinstance(raw[k],str) and raw[k]!='' for k in REQUEST_FIELDS),'Request values must be nonempty strings')
    require(raw['request_id'].strip()==raw['request_id'] and not any(ord(v)<32 for v in raw['request_id']),'invalid request_id')
    for key in ('predict_date','feature_date','target_date','daily_cutoff_key'):canonical_date(raw[key])
    require(raw['feature_date']<=raw['predict_date']<=raw['target_date'] and raw['feature_date']<raw['target_date'],'invalid Request date order')
    # The rule is defined at the supplied daily anchor, not at a guessed/replaced cutoff.
    require(raw['daily_cutoff_key']==raw['feature_date'],'this daily-anchor rule requires cutoff equal to feature_date')
    for key in ('weekly_cutoff_key','monthly_cutoff_key'):
        require(re.fullmatch(r'[0-9]{6}',raw[key]) is not None,'cutoff keys must be opaque six-digit strings')
    return {key:raw[key] for key in REQUEST_FIELDS}

def read_requests(path:Path,command:str)->list[dict[str,str]]:
    if command=='predict':
        with path.open('r',encoding='utf-8-sig') as f:raw=json.load(f,object_pairs_hook=unique_object)
        rows=[validate_request(raw)]
    else:
        header(path,REQUEST_FIELDS)
        with path.open('r',encoding='utf-8-sig',newline='') as f:
            rows=[validate_request(r) for r in csv.DictReader(f)]
    require(bool(rows),'empty request batch')
    ids=[r['request_id'] for r in rows]
    require(len(ids)==len(set(ids)),'duplicate request_id')
    return rows

class DataSource:
    def __init__(self,data_dir:Path,requests:list[dict[str,str]])->None:
        self.path=data_dir/'daily_output.csv'
        cols=header(self.path)
        require(cols[0]=='date' and set(FIELDS).issubset(cols),'missing consumed daily factor')
        catalog_path=data_dir/'factor_catalog.csv'
        header(catalog_path,('indicators_code','frequency','factor_version'))
        cat=pd.read_csv(catalog_path,dtype='string',keep_default_na=False)
        require(len(cat)>0 and cat.indicators_code.ne('').all() and not cat.indicators_code.duplicated().any(),'invalid factor catalog identity')
        cat=cat.set_index('indicators_code')
        for field in FIELDS:
            require(field in cat.index and cat.at[field,'frequency']=='daily' and cat.at[field,'factor_version']=='V1.0','consumed factor version/frequency mismatch')
        # Date-only metadata first. Business numbers beyond the greatest supplied cutoff are never parsed.
        axis=pd.read_csv(self.path,usecols=['date'],dtype='string',keep_default_na=False).date.tolist()
        require(bool(axis),'empty daily axis')
        for value in axis:canonical_date(value)
        require(len(axis)==len(set(axis)) and all(a<b for a,b in zip(axis,axis[1:])),'daily dates must be unique and ascending')
        self.axis=axis;self.positions={value:i for i,value in enumerate(axis)}
        calendar_path=data_dir/'api_wind_date.csv'
        header(calendar_path,('rdate','week_id'))
        cal=pd.read_csv(calendar_path,dtype='string',keep_default_na=False)
        require(len(cal)>0 and not cal.rdate.duplicated().any(),'duplicate or empty authoritative calendar')
        for value in cal.rdate:canonical_date(value)
        require(cal.week_id.str.fullmatch(r'[0-9]{6}').all(),'invalid opaque calendar week_id')
        mapping=dict(zip(cal.rdate,cal.week_id))
        self.ends={}
        for r in requests:
            cutoff=r['daily_cutoff_key']
            require(cutoff in self.positions,'daily cutoff absent')
            require(cutoff in mapping and mapping[cutoff]==r['weekly_cutoff_key'],'calendar week/cutoff mismatch')
            self.ends[cutoff]=self.positions[cutoff]+1
        self.end=max(self.ends.values())
        require(all(d in mapping for d in axis[:self.end]),'daily observation absent from authoritative calendar')
        self.raw=self.read_prefix(self.end)

    def read_prefix(self,end:int)->pd.DataFrame:
        frame=pd.read_csv(self.path,usecols=['date',*FIELDS],nrows=end,dtype={f:'float64' for f in FIELDS})
        require(frame.date.astype(str).tolist()==self.axis[:end],'numeric prefix and date axis mismatch')
        require(not np.isinf(frame[list(FIELDS)].to_numpy('float64')).any(),'infinite consumed business value')
        frame.index=pd.DatetimeIndex(pd.to_datetime(frame.pop('date'),format='%Y-%m-%d',errors='raise'))
        return frame

def bar_features(raw:pd.DataFrame,fields:tuple[str,...],volume_required:bool=True)->tuple[np.ndarray,np.ndarray]:
    o,h,l,c,v=(raw[name].shift(CONFIG['lag']) for name in fields)
    valid=(h>=l)&(h>=c)&(c>=l)&(h>=o)&(o>=l)
    if volume_required:valid=valid&(v>0)
    scale=c.diff().rolling(CONFIG['volatility_window'],min_periods=CONFIG['volatility_min_periods']).std(ddof=CONFIG['standard_deviation_ddof']).shift(1).replace(0,np.nan)
    relative=v/v.rolling(CONFIG['volume_window'],min_periods=CONFIG['volume_min_periods']).median().shift(1).replace(0,np.nan)
    intraday=((c-o)/scale).clip(-CONFIG['intraday_clip'],CONFIG['intraday_clip']).where(valid)
    volume=relative.clip(CONFIG['volume_clip_low'],CONFIG['volume_clip_high']).where(valid)
    return intraday.to_numpy('float64'),volume.to_numpy('float64')

def scores(raw:pd.DataFrame)->np.ndarray:
    cash_intraday,_=bar_features(raw,CASH,volume_required=False)
    future_intraday,relative_volume=bar_features(raw,FUTURE)
    oi_change=raw['TCFE000I'].shift(CONFIG['lag']).diff().to_numpy('float64')
    with np.errstate(invalid='ignore'):
        pressure=np.where(oi_change<0,future_intraday*np.sqrt(relative_volume),0.)
    valid=np.isfinite(cash_intraday)&np.isfinite(future_intraday)&np.isfinite(oi_change)
    pressure=np.where(valid,pressure,np.nan).clip(-CONFIG['pressure_clip'],CONFIG['pressure_clip'])
    return (pd.Series(pressure).rolling(CONFIG['window'],min_periods=CONFIG['window']).sum()/np.sqrt(CONFIG['window'])).to_numpy('float64')

def one_result(r:dict[str,str],score:float)->dict:
    require(np.isfinite(score),'insufficient or invalid source history for fixed rule')
    direction=int(np.sign(score)) if abs(score)>CONFIG['threshold'] else 0
    # Original operational freeze bridge; never retroactively execute the five requests.
    if r['feature_date']<='2025-12-31' and r['target_date']>='2026-01-01':direction=0
    require(direction in (-1,0,1),'invalid model direction')
    return {**{key:r[key] for key in RESULT_FIELDS[:-1]},'predicted_direction':direction}

def calculate(requests:list[dict[str,str]],data_dir:Path)->list[dict]:
    source=DataSource(data_dir,requests)
    seq=scores(source.raw)
    results=[one_result(r,float(seq[source.ends[r['daily_cutoff_key']]-1])) for r in requests]
    # Independent per-cutoff file reads for first/middle/last, not slices of the shared calculation.
    for index in sorted({0,len(requests)//2,len(requests)-1}):
        r=requests[index];end=source.ends[r['daily_cutoff_key']]
        independent=float(scores(source.read_prefix(end))[-1])
        require(independent==float(seq[end-1]),'batch score differs from independent cutoff calculation')
        require(one_result(r,independent)==results[index],'batch result differs from independent cutoff calculation')
    return results

def output_paths(output:Path,data_dir:Path,request_path:Path)->tuple[Path,Path]:
    output=output.resolve();data_dir=data_dir.resolve();request_path=request_path.resolve()
    require(not output.is_relative_to(data_dir) and output!=request_path and output not in (Path(__file__).resolve(),Path(__file__).with_suffix('.json').resolve()),'output overlaps protected input/code')
    require(output.parent.is_dir() and not output.is_symlink(),'invalid output parent or symlink')
    temporary=output.with_name(output.name+'.tmp')
    require(temporary!=request_path and not temporary.is_symlink(),'temporary output overlaps protected input')
    for path in (output,temporary):
        if path.exists():
            require(path.is_file(),'output is not a regular file')
            path.unlink()
    return output,temporary

def write_output(output:Path,temporary:Path,results:list[dict],command:str)->None:
    with temporary.open('x',encoding='utf-8',newline='') as f:
        if command=='predict':json.dump(results[0],f,ensure_ascii=False,allow_nan=False)
        else:
            writer=csv.DictWriter(f,fieldnames=RESULT_FIELDS,lineterminator='\n')
            writer.writeheader();writer.writerows(results)
        f.flush();os.fsync(f.fileno())
    os.replace(temporary,output)

class Parser(argparse.ArgumentParser):
    def print_help(self,file=None)->None:super().print_help(file=sys.stderr)

def main()->int:
    p=Parser(description='Frozen lag1 position-unwind rule; original no-action semantic exception is disclosed.')
    sub=p.add_subparsers(dest='command',required=True,parser_class=Parser)
    for name,key in [('predict','request'),('backtest','requests')]:
        q=sub.add_parser(name);q.add_argument('--'+key,type=Path,required=True);q.add_argument('--data-dir',type=Path,required=True);q.add_argument('--output',type=Path,required=True)
    args=p.parse_args();output=None;temporary=None
    try:
        request_path=getattr(args,'request' if args.command=='predict' else 'requests')
        output,temporary=output_paths(args.output,args.data_dir,request_path)
        requests=read_requests(request_path,args.command)
        results=calculate(requests,args.data_dir)
        write_output(output,temporary,results,args.command)
        LOG.info('completed %s: %d requests',args.command,len(results))
        return 0
    except Exception as exc:
        for path in (temporary,output):
            if path is not None and path.is_file():path.unlink()
        LOG.error('%s: %s',type(exc).__name__,exc)
        return 1

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    sys.exit(main())
