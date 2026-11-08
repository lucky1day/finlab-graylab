#!/usr/bin/env python3
"""Self-contained frozen 5Y weekly-close scheme, Blackbox V2 Contract1.0."""
from __future__ import annotations

import argparse
import csv
from datetime import date
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Sequence
import warnings

for _thread_name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_thread_name] = "1"

import numpy as np
import pandas as pd
import lightgbm as lgb
import xgboost as xgb
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

SEED = 20261104
REF_RANDOM_STATE = 20260901
NEW_RANDOM_STATE = 20261104
TARGET = "TB5YWI0C"
WEEKLY_TARGET = "TB5YWI3C"
YIELDS = ["TB1YWI0C", "TB3YWI0C", "TB5YWI0C", "TB7YWI0C", "TB0YWI0C"]
SCHEME_ID = 'weekly_5y_vote2808b6_sop_v3'
ALGORITHM = {'kind': 'ensemble',
 'members': [{'kind': 'logistic', 'group': 'union_joint', 'window': 104, 'C': 0.3},
             {'kind': 'reference',
              'config': {'scheme_id': 'weekly_5y_full_action_lowcorr03_v3',
                         'target': 'TB5YWI0C',
                         'weekly_target': 'TB5YWI3C',
                         'quorum': 1,
                         'daily_columns': ['TB0YWI0C', 'TB1YWI0C', 'TB3YWI0C', 'TB5YWI0C', 'TB7YWI0C'],
                         'weekly_columns': ['CSI26902',
                                            'G0100893',
                                            'HWM00007',
                                            'HWM00018',
                                            'HWW00005',
                                            'HWW00010',
                                            'L0147566',
                                            'M0161682',
                                            'M0161686',
                                            'M0217126',
                                            'M1024902',
                                            'M1332208',
                                            'RSI010YW',
                                            'S5021697',
                                            'S5133394',
                                            'S5134688',
                                            'S5134689',
                                            'S5134691',
                                            'S5440890',
                                            'S5441858',
                                            'S5442122',
                                            'S5442127',
                                            'S5446171',
                                            'S5458286',
                                            'S5458287',
                                            'S5470009',
                                            'S5715671',
                                            'S5811163',
                                            'S5811176',
                                            'SH069802',
                                            'TB5YWI3C',
                                            'TCFE002A',
                                            'TCFE002V',
                                            'TCFE020I',
                                            'TCFE02VR',
                                            'TCFE03WR',
                                            'ZO000004',
                                            'ZO000017',
                                            'ZO000035'],
                         'members': [{'config_id': 'lgbm_0097',
                                      'family': 'lgbm',
                                      'train_window': 208,
                                      'params': {'n_estimators': 120,
                                                 'learning_rate': 0.06,
                                                 'num_leaves': 8,
                                                 'min_child_samples': 20,
                                                 'reg_alpha': 0.5,
                                                 'reg_lambda': 3.0},
                                      'reject': 0.0,
                                      'features': ['core::vol4',
                                                   'core::vol3',
                                                   'core::spread_TB0YWI0C_TB5YWI0C',
                                                   'core::vol26',
                                                   'core::vol8',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d1',
                                                   'core::z3',
                                                   'core::target_sign2',
                                                   'core::vol13',
                                                   'core::spread_TB0YWI0C_TB3YWI0C',
                                                   'core::spread_TB0YWI0C_TB1YWI0C_d1',
                                                   'core::spread_TB5YWI0C_TB1YWI0C_d1',
                                                   'core::spread_TB7YWI0C_TB3YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB5YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB3YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB5YWI0C_d13',
                                                   'core::vol52',
                                                   'core::spread_TB7YWI0C_TB3YWI0C',
                                                   'core::target_diff1',
                                                   'core::target_diff8',
                                                   'core::target_sign6',
                                                   'core::target_sign3',
                                                   'core::target_sign1',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d4',
                                                   'core::mom8',
                                                   'core::target_sign52',
                                                   'core::z13',
                                                   'core::spread_TB0YWI0C_TB3YWI0C_d13',
                                                   'core::z8',
                                                   'core::spread_TB5YWI0C_TB1YWI0C_d4',
                                                   'core::target_diff2',
                                                   'core::z4',
                                                   'core::z52',
                                                   'core::spread_TB0YWI0C_TB1YWI0C_d4',
                                                   'core::target_diff3',
                                                   'core::mom3',
                                                   'core::target_diff4',
                                                   'core::target_diff26',
                                                   'core::mom4',
                                                   'core::spread_TB5YWI0C_TB3YWI0C',
                                                   'core::mom26',
                                                   'core::target_diff52',
                                                   'core::mom52',
                                                   'core::spread_TB7YWI0C_TB3YWI0C_d13',
                                                   'core::spread_TB5YWI0C_TB1YWI0C',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d13',
                                                   'core::spread_TB0YWI0C_TB5YWI0C_d1',
                                                   'core::z26',
                                                   'core::target_diff6',
                                                   'core::target_sign8',
                                                   'w_diff4::S5442127',
                                                   'w_diff13::M1332208',
                                                   'w_raw::TCFE020I',
                                                   'w_diff1::S5133394',
                                                   'w_diff2::S5442127',
                                                   'w_diff13::TCFE020I',
                                                   'w_diff13::S5458287',
                                                   'w_diff4::S5134688',
                                                   'w_diff4::S5442122',
                                                   'w_diff4::TCFE03WR',
                                                   'w_diff4::S5470009',
                                                   'w_diff4::S5715671',
                                                   'w_diff1::ZO000035',
                                                   'w_diff4::G0100893',
                                                   'w_diff1::TCFE002A',
                                                   'w_diff4::M0161682',
                                                   'w_diff4::S5446171',
                                                   'w_diff4::S5811176',
                                                   'w_diff1::TCFE02VR',
                                                   'w_diff1::S5458286',
                                                   'w_diff4::ZO000017',
                                                   'w_diff13::M1024902',
                                                   'w_raw::L0147566',
                                                   'w_diff13::SH069802',
                                                   'w_diff2::S5470009',
                                                   'w_diff2::M0161686',
                                                   'w_diff1::TCFE002V',
                                                   'w_diff13::S5134689',
                                                   'w_diff1::S5021697',
                                                   'w_diff4::S5441858',
                                                   'w_diff13::HWW00005',
                                                   'w_diff2::RSI010YW',
                                                   'w_raw::HWW00005',
                                                   'w_raw::RSI010YW',
                                                   'w_diff4::HWM00007',
                                                   'w_diff2::S5133394',
                                                   'w_diff2::S5811163',
                                                   'w_diff1::TCFE020I',
                                                   'w_raw::HWW00010',
                                                   'w_diff4::CSI26902',
                                                   'w_diff1::M0217126',
                                                   'w_diff13::HWM00018',
                                                   'w_diff13::S5134691',
                                                   'w_diff4::ZO000004',
                                                   'w_diff1::HWW00005',
                                                   'w_diff1::S5440890']},
                                     {'config_id': 'rule_0005',
                                      'family': 'rule',
                                      'train_window': 208,
                                      'params': {'rank': 5},
                                      'reject': 0.0,
                                      'features': ['core::vol4',
                                                   'core::vol3',
                                                   'core::spread_TB0YWI0C_TB5YWI0C',
                                                   'core::vol26',
                                                   'core::vol8',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d1']},
                                     {'config_id': 'rule_0019',
                                      'family': 'rule',
                                      'train_window': 208,
                                      'params': {'rank': 19},
                                      'reject': 0.0,
                                      'features': ['core::vol4',
                                                   'core::vol3',
                                                   'core::spread_TB0YWI0C_TB5YWI0C',
                                                   'core::vol26',
                                                   'core::vol8',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d1',
                                                   'core::z3',
                                                   'core::target_sign2',
                                                   'core::vol13',
                                                   'core::spread_TB0YWI0C_TB3YWI0C',
                                                   'core::spread_TB0YWI0C_TB1YWI0C_d1',
                                                   'core::spread_TB5YWI0C_TB1YWI0C_d1',
                                                   'core::spread_TB7YWI0C_TB3YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB5YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB3YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB5YWI0C_d13',
                                                   'core::vol52',
                                                   'core::spread_TB7YWI0C_TB3YWI0C',
                                                   'core::target_diff1',
                                                   'core::target_diff8']}]}},
             {'kind': 'reference',
              'config': {'scheme_id': 'weekly_5y_full_action_vote0536_v3',
                         'target': 'TB5YWI0C',
                         'weekly_target': 'TB5YWI3C',
                         'quorum': 1,
                         'daily_columns': ['90000017',
                                           'BIAS10YD',
                                           'CCI10Y0D',
                                           'CSI26901',
                                           'CSI64301',
                                           'DS000001',
                                           'DS000002',
                                           'DT000001',
                                           'G1235664',
                                           'M0332208',
                                           'MACDD5YD',
                                           'N2810001',
                                           'NISHF01C',
                                           'RSV5Y00D',
                                           'S0029752',
                                           'S0105897',
                                           'S0142908',
                                           'S2707384',
                                           'S2707385',
                                           'S5005830',
                                           'S5005865',
                                           'S5005984',
                                           'S5006004',
                                           'S5006011',
                                           'S5470300',
                                           'S5470302',
                                           'S5470303',
                                           'S5470305',
                                           'S5470316',
                                           'S5470318',
                                           'S5470333',
                                           'S5470364',
                                           'S5470395',
                                           'S5470444',
                                           'S5470445',
                                           'S5474439',
                                           'S5474441',
                                           'S5801724',
                                           'S5811336',
                                           'SH069801',
                                           'SI801790',
                                           'TB0YWI0C',
                                           'TB0YWIPC',
                                           'TB1YWI0C',
                                           'TB3YWI0C',
                                           'TB5YWI0C',
                                           'TB7YWI0C'],
                         'weekly_columns': ['BBI5Y00W',
                                            'BBIB5Y0W',
                                            'CSI26902',
                                            'EMA125YW',
                                            'G0100884',
                                            'G0100891',
                                            'G0100893',
                                            'G0200884',
                                            'G0200886',
                                            'G0200887',
                                            'HWC00001',
                                            'HWM00003',
                                            'HWM00004',
                                            'HWM00007',
                                            'HWM00015',
                                            'HWM00017',
                                            'HWM00018',
                                            'HWM00019',
                                            'HWS00002',
                                            'HWW00005',
                                            'HWW00010',
                                            'KDJ010YW',
                                            'L0147566',
                                            'M0161679',
                                            'M0161682',
                                            'M0161686',
                                            'M0217126',
                                            'M0220202',
                                            'M0241371',
                                            'M1024902',
                                            'M1332208',
                                            'MACDD0YW',
                                            'RSI010YW',
                                            'S0000070',
                                            'S0110144',
                                            'S0167795',
                                            'S0180497',
                                            'S5021682',
                                            'S5021697',
                                            'S5133394',
                                            'S5133411',
                                            'S5133884',
                                            'S5134688',
                                            'S5134689',
                                            'S5134691',
                                            'S5440890',
                                            'S5441642',
                                            'S5441857',
                                            'S5441858',
                                            'S5442122',
                                            'S5442127',
                                            'S5442372',
                                            'S5444097',
                                            'S5444190',
                                            'S5444191',
                                            'S5446171',
                                            'S5458286',
                                            'S5458287',
                                            'S5470009',
                                            'S5475759',
                                            'S5479504',
                                            'S5713286',
                                            'S5713338',
                                            'S5715671',
                                            'S5811163',
                                            'S5811172',
                                            'S5811176',
                                            'S6949777',
                                            'SH069802',
                                            'TB1YWI2V',
                                            'TB5YWI2C',
                                            'TB5YWI3C',
                                            'TCFE002A',
                                            'TCFE002V',
                                            'TCFE020I',
                                            'TCFE02VR',
                                            'TCFE03WR',
                                            'X0100205',
                                            'ZO000004',
                                            'ZO000006',
                                            'ZO000007',
                                            'ZO000011',
                                            'ZO000017',
                                            'ZO000019',
                                            'ZO000035'],
                         'members': [{'config_id': 'logistic_0004',
                                      'family': 'logistic',
                                      'train_window': 208,
                                      'params': {'C': 0.3, 'class_weight': 'balanced'},
                                      'reject': 0.0,
                                      'features': ['core::vol4',
                                                   'core::vol3',
                                                   'core::spread_TB0YWI0C_TB5YWI0C',
                                                   'core::vol26',
                                                   'core::vol8',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d1',
                                                   'core::z3',
                                                   'core::target_sign2',
                                                   'core::vol13',
                                                   'core::spread_TB0YWI0C_TB3YWI0C',
                                                   'core::spread_TB0YWI0C_TB1YWI0C_d1',
                                                   'core::spread_TB5YWI0C_TB1YWI0C_d1',
                                                   'core::spread_TB7YWI0C_TB3YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB5YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB3YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB5YWI0C_d13',
                                                   'core::vol52',
                                                   'core::spread_TB7YWI0C_TB3YWI0C',
                                                   'core::target_diff1',
                                                   'core::target_diff8',
                                                   'core::target_sign6',
                                                   'core::target_sign3',
                                                   'core::target_sign1',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d4',
                                                   'core::mom8',
                                                   'core::target_sign52',
                                                   'core::z13',
                                                   'core::spread_TB0YWI0C_TB3YWI0C_d13',
                                                   'core::z8',
                                                   'core::spread_TB5YWI0C_TB1YWI0C_d4',
                                                   'core::target_diff2',
                                                   'core::z4',
                                                   'core::z52',
                                                   'core::spread_TB0YWI0C_TB1YWI0C_d4',
                                                   'core::target_diff3',
                                                   'core::mom3',
                                                   'core::target_diff4',
                                                   'core::target_diff26',
                                                   'core::mom4',
                                                   'core::spread_TB5YWI0C_TB3YWI0C',
                                                   'core::mom26',
                                                   'core::target_diff52',
                                                   'core::mom52',
                                                   'core::spread_TB7YWI0C_TB3YWI0C_d13',
                                                   'core::spread_TB5YWI0C_TB1YWI0C',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d13',
                                                   'core::spread_TB0YWI0C_TB5YWI0C_d1',
                                                   'core::z26',
                                                   'core::target_diff6',
                                                   'core::target_sign8',
                                                   'd_raw::S5005984',
                                                   'd_raw::S5006004',
                                                   'd_diff13::S5470302',
                                                   'd_diff4::S5005830',
                                                   'd_diff13::M0332208',
                                                   'd_diff4::S5470333',
                                                   'd_diff4::S5470300',
                                                   'd_diff1::S5474441',
                                                   'd_diff1::S5470333',
                                                   'd_diff4::S5470303',
                                                   'd_raw::S5005830',
                                                   'd_diff13::CSI64301',
                                                   'd_raw::TB0YWIPC',
                                                   'd_diff4::S5470364',
                                                   'd_diff13::NISHF01C',
                                                   'd_raw::S5006011',
                                                   'd_diff4::CSI26901',
                                                   'd_diff1::N2810001',
                                                   'd_diff13::SH069801',
                                                   'd_diff13::S5005830',
                                                   'd_diff4::S5470318',
                                                   'd_raw::RSV5Y00D',
                                                   'd_diff2::S5801724',
                                                   'd_diff2::90000017',
                                                   'd_diff4::G1235664',
                                                   'd_raw::S5470302',
                                                   'd_diff13::S5811336',
                                                   'd_diff13::S0105897',
                                                   'd_diff4::S0029752',
                                                   'd_diff13::DS000001',
                                                   'd_diff13::DS000002',
                                                   'd_raw::MACDD5YD',
                                                   'd_raw::S5005865',
                                                   'd_raw::S0142908',
                                                   'd_diff13::S5470444',
                                                   'd_diff13::DT000001',
                                                   'd_diff1::S5474439',
                                                   'd_diff13::S5470305',
                                                   'd_diff1::S5470316',
                                                   'd_diff1::SI801790',
                                                   'd_diff4::S2707385',
                                                   'd_diff2::CCI10Y0D',
                                                   'd_raw::BIAS10YD',
                                                   'd_diff13::S5470445',
                                                   'd_diff4::S2707384',
                                                   'd_diff4::S5470395']},
                                     {'config_id': 'xgb_0115',
                                      'family': 'xgb',
                                      'train_window': 208,
                                      'params': {'n_estimators': 200,
                                                 'learning_rate': 0.1,
                                                 'max_depth': 1,
                                                 'min_child_weight': 3,
                                                 'subsample': 1.0,
                                                 'colsample_bytree': 1.0},
                                      'reject': 0.0,
                                      'features': ['core::vol4',
                                                   'core::vol3',
                                                   'core::spread_TB0YWI0C_TB5YWI0C',
                                                   'core::vol26',
                                                   'core::vol8',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d1',
                                                   'core::z3',
                                                   'core::target_sign2',
                                                   'core::vol13',
                                                   'core::spread_TB0YWI0C_TB3YWI0C',
                                                   'core::spread_TB0YWI0C_TB1YWI0C_d1',
                                                   'core::spread_TB5YWI0C_TB1YWI0C_d1',
                                                   'core::spread_TB7YWI0C_TB3YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB5YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB3YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB5YWI0C_d13',
                                                   'core::vol52',
                                                   'core::spread_TB7YWI0C_TB3YWI0C',
                                                   'core::target_diff1',
                                                   'core::target_diff8',
                                                   'core::target_sign6',
                                                   'core::target_sign3',
                                                   'core::target_sign1',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d4',
                                                   'core::mom8',
                                                   'core::target_sign52',
                                                   'core::z13',
                                                   'core::spread_TB0YWI0C_TB3YWI0C_d13',
                                                   'core::z8',
                                                   'core::spread_TB5YWI0C_TB1YWI0C_d4',
                                                   'core::target_diff2',
                                                   'core::z4',
                                                   'core::z52',
                                                   'core::spread_TB0YWI0C_TB1YWI0C_d4',
                                                   'core::target_diff3',
                                                   'core::mom3',
                                                   'core::target_diff4',
                                                   'core::target_diff26',
                                                   'core::mom4',
                                                   'core::spread_TB5YWI0C_TB3YWI0C',
                                                   'core::mom26',
                                                   'core::target_diff52',
                                                   'core::mom52',
                                                   'core::spread_TB7YWI0C_TB3YWI0C_d13',
                                                   'core::spread_TB5YWI0C_TB1YWI0C',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d13',
                                                   'core::spread_TB0YWI0C_TB5YWI0C_d1',
                                                   'core::z26',
                                                   'core::target_diff6',
                                                   'core::target_sign8',
                                                   'w_diff4::S5442127',
                                                   'w_diff13::M1332208',
                                                   'w_raw::TCFE020I',
                                                   'w_diff1::S5133394',
                                                   'w_diff2::S5442127',
                                                   'w_diff13::TCFE020I',
                                                   'w_diff13::S5458287',
                                                   'w_diff4::S5134688',
                                                   'w_diff4::S5442122',
                                                   'w_diff4::TCFE03WR',
                                                   'w_diff4::S5470009',
                                                   'w_diff4::S5715671',
                                                   'w_diff1::ZO000035',
                                                   'w_diff4::G0100893',
                                                   'w_diff1::TCFE002A',
                                                   'w_diff4::M0161682',
                                                   'w_diff4::S5446171',
                                                   'w_diff4::S5811176',
                                                   'w_diff1::TCFE02VR',
                                                   'w_diff1::S5458286',
                                                   'w_diff4::ZO000017',
                                                   'w_diff13::M1024902',
                                                   'w_raw::L0147566',
                                                   'w_diff13::SH069802',
                                                   'w_diff2::S5470009',
                                                   'w_diff2::M0161686',
                                                   'w_diff1::TCFE002V',
                                                   'w_diff13::S5134689',
                                                   'w_diff1::S5021697',
                                                   'w_diff4::S5441858',
                                                   'w_diff13::HWW00005',
                                                   'w_diff2::RSI010YW',
                                                   'w_raw::HWW00005',
                                                   'w_raw::RSI010YW',
                                                   'w_diff4::HWM00007',
                                                   'w_diff2::S5133394',
                                                   'w_diff2::S5811163',
                                                   'w_diff1::TCFE020I',
                                                   'w_raw::HWW00010',
                                                   'w_diff4::CSI26902',
                                                   'w_diff1::M0217126',
                                                   'w_diff13::HWM00018',
                                                   'w_diff13::S5134691',
                                                   'w_diff4::ZO000004',
                                                   'w_diff1::HWW00005',
                                                   'w_diff1::S5440890',
                                                   'w_raw::S5021682',
                                                   'w_diff1::M0241371',
                                                   'w_diff13::S6949777',
                                                   'w_diff1::S5441642',
                                                   'w_diff1::S5713286',
                                                   'w_diff13::S5479504',
                                                   'w_diff4::G0200886',
                                                   'w_diff1::ZO000017',
                                                   'w_diff4::S5442372',
                                                   'w_diff4::G0100884',
                                                   'w_diff13::S0000070',
                                                   'w_diff2::TB5YWI2C',
                                                   'w_raw::S5444190',
                                                   'w_diff4::HWM00019',
                                                   'w_diff1::ZO000006',
                                                   'w_diff13::S5444097',
                                                   'w_raw::S5134689',
                                                   'w_raw::S0167795',
                                                   'w_diff1::SH069802',
                                                   'w_raw::S5444191',
                                                   'w_diff2::M0220202',
                                                   'w_diff4::S5441642',
                                                   'w_diff1::S0110144',
                                                   'w_diff4::HWM00015',
                                                   'w_diff2::S5133884',
                                                   'w_diff4::TCFE020I',
                                                   'w_diff4::HWM00003',
                                                   'w_diff2::S0180497',
                                                   'w_diff1::S5713338',
                                                   'w_diff1::X0100205',
                                                   'w_diff13::HWW00010',
                                                   'w_diff2::KDJ010YW',
                                                   'w_diff4::HWM00004',
                                                   'w_diff2::S5440890',
                                                   'w_diff2::ZO000011',
                                                   'w_diff1::HWS00002',
                                                   'w_diff2::S5458286',
                                                   'w_raw::M0241371',
                                                   'w_diff1::ZO000007',
                                                   'w_diff1::ZO000011',
                                                   'w_raw::S5475759',
                                                   'w_diff2::S5441642',
                                                   'w_diff1::G0200884',
                                                   'w_diff4::ZO000019',
                                                   'w_diff13::RSI010YW',
                                                   'w_diff2::S5442122',
                                                   'w_diff4::G0200887',
                                                   'w_diff4::ZO000011',
                                                   'w_diff2::S5134688',
                                                   'w_diff13::HWM00017',
                                                   'w_diff1::S5133411',
                                                   'w_diff13::TB1YWI2V',
                                                   'w_diff13::M0241371',
                                                   'w_diff1::HWC00001',
                                                   'w_diff4::S5441857',
                                                   'w_raw::BBI5Y00W',
                                                   'w_raw::MACDD0YW',
                                                   'w_diff4::G0100891',
                                                   'w_raw::EMA125YW',
                                                   'w_raw::BBIB5Y0W',
                                                   'w_raw::M0161679',
                                                   'w_diff2::S5811172',
                                                   'w_diff2::S5021697',
                                                   'w_diff4::M1024902']},
                                     {'config_id': 'knn_0150',
                                      'family': 'knn',
                                      'train_window': 0,
                                      'params': {'n_neighbors': 9, 'weights': 'uniform', 'p': 1},
                                      'reject': 0.0,
                                      'features': ['core::vol4',
                                                   'core::vol3',
                                                   'core::spread_TB0YWI0C_TB5YWI0C',
                                                   'core::vol26',
                                                   'core::vol8',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d1',
                                                   'core::z3',
                                                   'core::target_sign2',
                                                   'core::vol13',
                                                   'core::spread_TB0YWI0C_TB3YWI0C',
                                                   'core::spread_TB0YWI0C_TB1YWI0C_d1',
                                                   'core::spread_TB5YWI0C_TB1YWI0C_d1',
                                                   'core::spread_TB7YWI0C_TB3YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB5YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB3YWI0C_d4',
                                                   'core::spread_TB0YWI0C_TB5YWI0C_d13',
                                                   'core::vol52',
                                                   'core::spread_TB7YWI0C_TB3YWI0C',
                                                   'core::target_diff1',
                                                   'core::target_diff8',
                                                   'core::target_sign6',
                                                   'core::target_sign3',
                                                   'core::target_sign1',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d4',
                                                   'core::mom8',
                                                   'core::target_sign52',
                                                   'core::z13',
                                                   'core::spread_TB0YWI0C_TB3YWI0C_d13',
                                                   'core::z8',
                                                   'core::spread_TB5YWI0C_TB1YWI0C_d4',
                                                   'core::target_diff2',
                                                   'core::z4',
                                                   'core::z52',
                                                   'core::spread_TB0YWI0C_TB1YWI0C_d4',
                                                   'core::target_diff3',
                                                   'core::mom3',
                                                   'core::target_diff4',
                                                   'core::target_diff26',
                                                   'core::mom4',
                                                   'core::spread_TB5YWI0C_TB3YWI0C',
                                                   'core::mom26',
                                                   'core::target_diff52',
                                                   'core::mom52',
                                                   'core::spread_TB7YWI0C_TB3YWI0C_d13',
                                                   'core::spread_TB5YWI0C_TB1YWI0C',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d13',
                                                   'core::spread_TB0YWI0C_TB5YWI0C_d1',
                                                   'core::z26']},
                                     {'config_id': 'extra_trees_0047',
                                      'family': 'extra_trees',
                                      'train_window': 104,
                                      'params': {'n_estimators': 80,
                                                 'max_depth': None,
                                                 'min_samples_leaf': 20,
                                                 'max_features': 0.5},
                                      'reject': 0.0,
                                      'features': ['core::vol4',
                                                   'core::vol3',
                                                   'core::spread_TB0YWI0C_TB5YWI0C',
                                                   'core::vol26',
                                                   'core::vol8',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d1',
                                                   'core::z3',
                                                   'core::target_sign2',
                                                   'core::vol13',
                                                   'core::spread_TB0YWI0C_TB3YWI0C',
                                                   'core::spread_TB0YWI0C_TB1YWI0C_d1',
                                                   'core::spread_TB5YWI0C_TB1YWI0C_d1']},
                                     {'config_id': 'rule_0010',
                                      'family': 'rule',
                                      'train_window': 416,
                                      'params': {'rank': 10},
                                      'reject': 0.0,
                                      'features': ['core::vol4',
                                                   'core::vol3',
                                                   'core::spread_TB0YWI0C_TB5YWI0C',
                                                   'core::vol26',
                                                   'core::vol8',
                                                   'core::spread_TB3YWI0C_TB1YWI0C_d1',
                                                   'core::z3',
                                                   'core::target_sign2',
                                                   'core::vol13',
                                                   'core::spread_TB0YWI0C_TB3YWI0C',
                                                   'core::spread_TB0YWI0C_TB1YWI0C_d1']}]}}]}
DAILY_COLUMNS = ('90000017', 'BIAS10YD', 'CCI10Y0D', 'CSI26901', 'CSI64301', 'DS000001', 'DS000002', 'DT000001', 'G1235664', 'M0332208', 'M1006336', 'M1006337', 'MACDD5YD', 'N2810001', 'NISHF01C', 'RSV5Y00D', 'S0029752', 'S0105897', 'S0142908', 'S2707384', 'S2707385', 'S5005830', 'S5005865', 'S5005984', 'S5006004', 'S5006011', 'S5470300', 'S5470302', 'S5470303', 'S5470305', 'S5470316', 'S5470318', 'S5470333', 'S5470364', 'S5470395', 'S5470444', 'S5470445', 'S5474439', 'S5474441', 'S5801724', 'S5811336', 'SH069801', 'SI801790', 'TB0YWI0C', 'TB0YWIPC', 'TB1YWI0C', 'TB3YWI0C', 'TB5YWI0C', 'TB7YWI0C', 'TCFE000A', 'TCFE000C', 'TCFE000H', 'TCFE000I', 'TCFE000K', 'TCFE000L', 'TCFE000S', 'TCFE000V', 'TFCFE002', 'TFCFE003', 'TSCFE002', 'TSCFE003')
WEEKLY_COLUMNS = ('BBI5Y00W', 'BBIB5Y0W', 'CSI26902', 'EMA125YW', 'G0100884', 'G0100891', 'G0100893', 'G0200884', 'G0200886', 'G0200887', 'HWC00001', 'HWM00003', 'HWM00004', 'HWM00007', 'HWM00015', 'HWM00017', 'HWM00018', 'HWM00019', 'HWS00002', 'HWW00005', 'HWW00010', 'KDJ010YW', 'L0147566', 'M0161679', 'M0161682', 'M0161686', 'M0217126', 'M0220202', 'M0241371', 'M1024902', 'M1332208', 'MACDD0YW', 'RSI010YW', 'S0000070', 'S0110144', 'S0167795', 'S0180497', 'S5021682', 'S5021697', 'S5133394', 'S5133411', 'S5133884', 'S5134688', 'S5134689', 'S5134691', 'S5440890', 'S5441642', 'S5441857', 'S5441858', 'S5442122', 'S5442127', 'S5442372', 'S5444097', 'S5444190', 'S5444191', 'S5446171', 'S5458286', 'S5458287', 'S5470009', 'S5475759', 'S5479504', 'S5713286', 'S5713338', 'S5715671', 'S5811163', 'S5811172', 'S5811176', 'S6949777', 'SH069802', 'TB1YWI2V', 'TB5YWI2C', 'TB5YWI3C', 'TCFE002A', 'TCFE002V', 'TCFE020I', 'TCFE02VR', 'TCFE03WR', 'X0100205', 'ZO000004', 'ZO000006', 'ZO000007', 'ZO000011', 'ZO000017', 'ZO000019', 'ZO000035')
REQUEST_FIELDS = ("request_id", "predict_date", "feature_date", "target_date", "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key")
OUTPUT_FIELDS = ("request_id", "predict_date", "feature_date", "target_date", "predicted_direction")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
KEY_RE = re.compile(r"[0-9]{6}\Z")
LOG = logging.getLogger(__name__)

def _ref_features(endpoints: pd.DataFrame, weekly_slice: pd.DataFrame, members: list[dict]) -> pd.DataFrame:
    daily_numeric = endpoints.drop(columns=["date", "week_id"], errors="ignore").astype(float)
    daily_numeric.columns = [f"d_raw::{column}" for column in daily_numeric.columns]
    weekly_numeric = weekly_slice.set_index("week_id").reindex(
        endpoints["week_id"].astype(str)
    )
    weekly_numeric = weekly_numeric.reset_index(drop=True).astype(float)
    weekly_numeric.columns = [f"w_raw::{column}" for column in weekly_numeric.columns]
    blocks: list[pd.DataFrame] = [daily_numeric, weekly_numeric]
    for lag in (1, 2, 4, 13):
        daily_diff = daily_numeric.diff(lag)
        daily_diff.columns = [
            f"d_diff{lag}::{column.split('::', 1)[1]}" for column in daily_numeric.columns
        ]
        weekly_diff = weekly_numeric.diff(lag)
        weekly_diff.columns = [
            f"w_diff{lag}::{column.split('::', 1)[1]}" for column in weekly_numeric.columns
        ]
        blocks.extend([daily_diff, weekly_diff])

    current = pd.to_numeric(endpoints[TARGET], errors="coerce")
    core = pd.DataFrame(index=endpoints.index)
    for lag in (1, 2, 3, 4, 6, 8, 13, 26, 52):
        core[f"core::target_diff{lag}"] = current.diff(lag)
        core[f"core::target_sign{lag}"] = np.sign(current.diff(lag))
    change = current.diff()
    for window in (3, 4, 8, 13, 26, 52):
        minimum = max(2, window // 2)
        core[f"core::mom{window}"] = change.rolling(window, min_periods=minimum).sum()
        core[f"core::vol{window}"] = change.rolling(window, min_periods=minimum).std()
        mean = current.rolling(window, min_periods=minimum).mean()
        std = current.rolling(window, min_periods=minimum).std()
        core[f"core::z{window}"] = current.sub(mean).div(std.replace(0.0, np.nan))
    for left, right in (
        ("TB3YWI0C", "TB1YWI0C"),
        ("TB5YWI0C", "TB3YWI0C"),
        ("TB7YWI0C", "TB3YWI0C"),
        ("TB0YWI0C", "TB3YWI0C"),
        ("TB0YWI0C", "TB5YWI0C"),
        ("TB5YWI0C", "TB1YWI0C"),
        ("TB0YWI0C", "TB1YWI0C"),
    ):
        if left in endpoints and right in endpoints:
            spread = pd.to_numeric(endpoints[left], errors="coerce") - pd.to_numeric(
                endpoints[right], errors="coerce"
            )
            core[f"core::spread_{left}_{right}"] = spread
            for lag in (1, 4, 13):
                core[f"core::spread_{left}_{right}_d{lag}"] = spread.diff(lag)
    features = pd.concat([core, *blocks], axis=1).replace([np.inf, -np.inf], np.nan)
    required_features = {
        str(feature)
        for member in members
        for feature in member["features"]
    }
    missing = sorted(required_features.difference(features.columns))
    if missing:
        raise ValueError(f"frozen feature manifest cannot be built: {missing}")
    return features.astype(np.float32)

def _ref_member_probability(
    member: dict[str, Any],
    features: pd.DataFrame,
    labels: np.ndarray,
    eligible: np.ndarray,
) -> float:
    train_window = int(member["train_window"])
    use = eligible[-train_window:] if train_window > 0 else eligible
    if len(use) < 40 or np.unique(labels[use]).size < 2:
        raise ValueError(
            f"insufficient history for {member['config_id']}: at least 40 two-class labels are required"
        )
    columns = tuple(str(column) for column in member["features"])
    x = features.loc[:, columns].to_numpy(dtype=np.float32)
    yy = labels[use].astype(int)
    family = str(member["family"])
    params = dict(member["params"])
    if family == "rule":
        column = min(int(params["rank"]), x.shape[1] - 1)
        values = x[use, column].astype(float)
        median = float(np.nanmedian(values))
        scale = float(np.nanstd(values))
        current = float(x[-1, column])
        if not math.isfinite(scale) or scale < 1e-12 or not math.isfinite(current):
            raise ValueError("rule member has unusable feature history")
        centered = np.nan_to_num(values - median, nan=0.0)
        correlation = (
            float(np.corrcoef(centered, yy.astype(float))[0, 1])
            if np.std(centered) > 0
            else 0.0
        )
        orientation = 1.0 if correlation >= 0 else -1.0
        z_value = float(np.clip(orientation * (current - median) / scale, -8.0, 8.0))
        return 1.0 / (1.0 + math.exp(-z_value))
    if family == "logistic":
        classifier = LogisticRegression(
            C=float(params["C"]),
            class_weight=params["class_weight"],
            max_iter=1000,
            solver="liblinear",
            random_state=REF_RANDOM_STATE,
        )
        model = Pipeline(
            (("imputer", SimpleImputer(strategy="median")),
             ("scale", StandardScaler()),
             ("model", classifier))
        )
    elif family == "hist_gb":
        classifier = HistGradientBoostingClassifier(
            **params,
            random_state=REF_RANDOM_STATE,
        )
        model = Pipeline(
            (("imputer", SimpleImputer(strategy="median")), ("model", classifier))
        )
    elif family == "extra_trees":
        classifier = ExtraTreesClassifier(
            **params,
            class_weight="balanced",
            random_state=REF_RANDOM_STATE,
            n_jobs=1,
        )
        model = Pipeline(
            (("imputer", SimpleImputer(strategy="median")), ("model", classifier))
        )
    elif family == "xgb":
        classifier = xgb.XGBClassifier(
            **params,
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=REF_RANDOM_STATE,
            n_jobs=1,
            reg_alpha=0.2,
            reg_lambda=2.0,
            tree_method="hist",
        )
        model = Pipeline(
            (("imputer", SimpleImputer(strategy="median")), ("model", classifier))
        )
    elif family == "lgbm":
        classifier = lgb.LGBMClassifier(
            **params,
            objective="binary",
            verbosity=-1,
            deterministic=True,
            force_col_wise=True,
            random_state=REF_RANDOM_STATE,
            n_jobs=1,
            subsample=0.85,
            colsample_bytree=0.85,
        )
        model = Pipeline(
            (("imputer", SimpleImputer(strategy="median")), ("model", classifier))
        )
    elif family == "knn":
        if len(use) < int(params["n_neighbors"]):
            raise ValueError(f"insufficient history for {member['config_id']} KNN neighbors")
        classifier = KNeighborsClassifier(**params, n_jobs=1)
        model = Pipeline(
            (("imputer", SimpleImputer(strategy="median")),
             ("scale", StandardScaler()),
             ("model", classifier))
        )
    else:
        raise RuntimeError(f"unknown frozen member family: {family}")
    model.fit(x[use], (yy == 1).astype(int))
    probability = float(model.predict_proba(x[[-1]])[0, 1])
    if not math.isfinite(probability):
        raise RuntimeError(f"{member['config_id']} returned a non-finite probability")
    return probability

def _union_features(daily: pd.DataFrame, ep: pd.DataFrame) -> dict[str, pd.DataFrame]:
    ends = pd.DatetimeIndex(ep.date)
    cash, funding = _cash_funding(daily, ep)
    prices: dict[str, pd.Series] = {}
    weekly_returns = {}
    for prefix, code, settle_code in (("ts", "TSCFE002", "TSCFE003"), ("tf", "TFCFE002", "TFCFE003"), ("t", "TCFE000C", "TCFE000S")):
        p = np.log(daily[code].where(daily[code] > 0))
        weekly = pd.Series(p.reindex(ends).to_numpy(), index=ep.index)
        weekly_returns[prefix] = weekly.diff()
        for lag in (1, 2, 4, 13):
            prices[f"{prefix}_wreturn{lag}"] = weekly.diff(lag)
        for lag in (1, 5, 20):
            prices[f"{prefix}_dreturn{lag}"] = pd.Series(p.diff(lag).reindex(ends).to_numpy(), index=ep.index)
        for n in (5, 20):
            vol = p.diff().rolling(n).std()
            prices[f"{prefix}_dvol{n}"] = pd.Series(vol.reindex(ends).to_numpy(), index=ep.index)
        gap = (daily[code] - daily[settle_code]) / daily[code]
        prices[f"{prefix}_close_settle"] = pd.Series(gap.reindex(ends).to_numpy(), index=ep.index)
    for prefix in ("ts", "tf"):
        prices[f"{prefix}_minus_t"] = weekly_returns[prefix] - weekly_returns["t"]
        prices[f"{prefix}_minus_t4"] = prices[f"{prefix}_minus_t"].rolling(4).sum()
    y = ep["TB5YWI0C"]
    yvol = y.diff().rolling(13).std().replace(0, np.nan)
    fvol = weekly_returns["tf"].rolling(13).std().replace(0, np.nan)
    prices["cash_tf_divergence1"] = y.diff() / yvol + weekly_returns["tf"] / fvol
    prices["cash_tf_divergence4"] = y.diff(4) / yvol + weekly_returns["tf"].rolling(4).sum() / fvol
    week_number = daily.week_id.map(pd.Series(ep.index.to_numpy(), index=ep.week_id))
    raw = daily.loc[week_number.notna()].copy()
    raw["week_number"] = week_number.loc[week_number.notna()].to_numpy(dtype=int)
    grouped = raw.groupby("week_number", sort=True)
    wc = pd.Series(daily.TCFE000C.reindex(ends).to_numpy(), index=ep.index)
    wo = grouped.TCFE000K.first().reindex(ep.index)
    wh, wl = grouped.TCFE000H.max().reindex(ep.index), grouped.TCFE000L.min().reindex(ep.index)
    wv = grouped.TCFE000V.sum(min_count=1).reindex(ep.index)
    woi = pd.Series(daily.TCFE000I.reindex(ends).to_numpy(), index=ep.index)
    shape = pd.DataFrame({"t_week_range": (wh - wl) / wc, "t_week_body": (wc - wo) / wc,
                          "t_week_location": (2 * wc - wh - wl) / (wh - wl).replace(0, np.nan),
                          "t_upper_wick": (wh - np.maximum(wo, wc)) / wc,
                          "t_lower_wick": (np.minimum(wo, wc) - wl) / wc})
    activity = {}
    for name, series in (("volume", wv), ("oi", woi)):
        log = np.log(series.where(series > 0))
        for lag in (1, 4, 13):
            activity[f"t_week_{name}_d{lag}"] = log.diff(lag)
        for n in (4, 13):
            activity[f"t_week_{name}_z{n}"] = (log - log.rolling(n).mean()) / log.rolling(n).std().replace(0, np.nan)
    activity["t_week_turnover"] = wv / woi.where(woi > 0)
    for measure in ("TCFE000V", "TCFE000I", "TCFE000A"):
        log = np.log(daily[measure].where(daily[measure] > 0))
        for lag in (1, 5):
            activity[f"{measure}_d{lag}"] = pd.Series(log.diff(lag).reindex(ends).to_numpy(), index=ep.index)
        z = (log - log.rolling(20).mean()) / log.rolling(20).std().replace(0, np.nan)
        activity[f"{measure}_z20"] = pd.Series(z.reindex(ends).to_numpy(), index=ep.index)
    activity["t_price_oi_interaction"] = np.sign(weekly_returns["t"]) * activity["t_week_oi_d1"]
    activity["tf_price_t_volume_interaction"] = np.sign(weekly_returns["tf"]) * activity["t_week_volume_z13"]
    price = pd.DataFrame(prices)
    flow = pd.DataFrame(activity)
    groups = {"union_curve_price": pd.concat([cash, price], axis=1),
              "union_price_shape": pd.concat([cash.iloc[:, :7], price, shape], axis=1),
              "union_positioning": pd.concat([cash.iloc[:, :7], flow, price[["tf_wreturn1", "t_wreturn1", "cash_tf_divergence1"]]], axis=1),
              "union_funding_price": pd.concat([cash, funding, price], axis=1),
              "union_joint": pd.concat([cash, price, shape, flow, funding], axis=1)}
    return {name: frame.replace([np.inf, -np.inf], np.nan).astype("float32") for name, frame in groups.items()}


def _cash_funding(daily: pd.DataFrame, ep: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    y = ep[TARGET].astype(float)
    cash: dict[str, pd.Series] = {}
    for lag in (1, 2, 3, 4, 8, 13, 26):
        cash[f"yield_diff{lag}"] = y.diff(lag)
    for n in (4, 8, 13, 26):
        mean, std = y.rolling(n, min_periods=n).mean(), y.rolling(n, min_periods=n).std()
        cash[f"yield_z{n}"] = (y - mean) / std.replace(0, np.nan)
        cash[f"yield_vol{n}"] = y.diff().rolling(n, min_periods=n).std()
    for code in YIELDS:
        if code == TARGET:
            continue
        spread = ep[code] - y
        cash[f"spread_{code}"] = spread
        for lag in (1, 4, 13):
            cash[f"spread_{code}_d{lag}"] = spread.diff(lag)
    cash["butterfly_3_5_10"] = ep["TB3YWI0C"] + ep["TB0YWI0C"] - 2 * y
    cash["butterfly_change4"] = cash["butterfly_3_5_10"].diff(4)
    funding: dict[str, pd.Series] = {}
    for code in ("M1006336", "M1006337"):
        values = daily[code].shift(2)
        level = pd.Series(values.reindex(pd.DatetimeIndex(ep["date"])).to_numpy(), index=ep.index)
        funding[f"{code}_level"] = level
        funding[f"{code}_change1"] = level.diff()
        funding[f"{code}_change4"] = level.diff(4)
        funding[f"{code}_z13"] = (level - level.rolling(13).mean()) / level.rolling(13).std().replace(0, np.nan)
    funding["dr7_dr1"] = funding["M1006337_level"] - funding["M1006336_level"]
    funding["dr7_5y"] = funding["M1006337_level"] - y
    return pd.DataFrame(cash).astype("float32"), pd.DataFrame(funding).astype("float32")


def _date(value: str, field: str) -> date:
    if not DATE_RE.fullmatch(value):
        raise ValueError(f"{field} must use YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"invalid {field}")
    return parsed


def validate_request(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(REQUEST_FIELDS) or len(value) != 7:
        raise ValueError("Request must contain exactly seven contract fields")
    if any(not isinstance(value[k], str) for k in REQUEST_FIELDS):
        raise ValueError("All Request fields must be strings")
    if not value["request_id"].strip():
        raise ValueError("Blank request_id")
    feature, predict, target, cutoff = [_date(value[k], k) for k in ("feature_date", "predict_date", "target_date", "daily_cutoff_key")]
    if not (feature <= predict <= target and feature < target) or cutoff != feature:
        raise ValueError("Invalid date order or weekly feature/daily cutoff mismatch")
    for k in ("weekly_cutoff_key", "monthly_cutoff_key"):
        if not KEY_RE.fullmatch(value[k]):
            raise ValueError(f"Invalid opaque period key {k}")
    return dict(value)


def _unique(pairs: list[tuple[str, Any]]) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("Duplicate JSON field")
        out[key] = value
    return out


def _constant(value: str):
    raise ValueError(f"Nonstandard JSON numeric constant {value}")


def read_request(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig") as stream:
        return validate_request(json.load(stream, object_pairs_hook=_unique, parse_constant=_constant))


def read_requests(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        header = next(reader, [])
        if len(header) != 7 or set(header) != set(REQUEST_FIELDS):
            raise ValueError("Invalid requests CSV header")
        rows = []
        for row in reader:
            if len(row) != len(header):
                raise ValueError("Incomplete requests CSV row")
            rows.append(validate_request(dict(zip(header, row, strict=True))))
    if not rows or len({x["request_id"] for x in rows}) != len(rows):
        raise ValueError("Empty batch or duplicate request_id")
    return rows


def _read(data_dir: Path, filename: str, key: str, columns: Sequence[str]) -> pd.DataFrame:
    path = data_dir / filename
    if not path.is_file() or path.resolve().parent != data_dir.resolve():
        raise ValueError(f"Required input absent or outside data-dir: {filename}")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        header = next(csv.reader(stream), [])
    if not header or header[0] != key or len(set(header)) != len(header) or not set(columns).issubset(header):
        raise ValueError(f"Invalid header or required columns missing: {filename}")
    table = pd.read_csv(path, usecols=[key, *columns], dtype="string", keep_default_na=False, encoding="utf-8-sig")
    table = table[[key, *columns]]
    if table.empty or table[key].str.strip().eq("").any() or table[key].duplicated().any():
        raise ValueError(f"Empty or duplicate time key: {filename}")
    return table


def load_data(data_dir: Path) -> dict:
    if not data_dir.is_dir():
        raise ValueError("data-dir must be a directory")
    calendar = _read(data_dir, "api_wind_date.csv", "rdate", ["week_id"])
    if list(pd.read_csv(data_dir / "api_wind_date.csv", nrows=0)) != ["rdate", "week_id"]:
        raise ValueError("Calendar must have exact rdate,week_id fields")
    daily = _read(data_dir, "daily_output.csv", "date", DAILY_COLUMNS)
    weekly = _read(data_dir, "weekly_output.csv", "week_id", WEEKLY_COLUMNS)
    catalog = _read(data_dir, "factor_catalog.csv", "indicators_code", ["frequency", "factor_version"])
    if list(pd.read_csv(data_dir / "factor_catalog.csv", nrows=0)) != ["indicators_code", "frequency", "factor_version"]:
        raise ValueError("factor_catalog must have exactly three contract fields")
    cmap = catalog.set_index("indicators_code")
    for frequency, fields in [("daily", DAILY_COLUMNS), ("weekly", WEEKLY_COLUMNS)]:
        if not set(fields).issubset(cmap.index):
            raise ValueError("Required factor absent from catalog")
        selected = cmap.loc[list(fields)]
        if not selected["frequency"].eq(frequency).all() or not selected["factor_version"].eq("V1.0").all():
            raise ValueError("Frozen source factor version/frequency mismatch")
    for table, field in [(calendar, "rdate"), (daily, "date")]:
        if not table[field].str.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}").all():
            raise ValueError("DataBridge daily/calendar keys must be canonical dates")
        dates = pd.to_datetime(table[field], format="%Y-%m-%d", errors="raise")
        if not dates.is_monotonic_increasing:
            raise ValueError("Daily/calendar keys not strictly ordered")
    if not calendar["week_id"].str.fullmatch(r"[0-9]{6}").all() or not weekly["week_id"].str.fullmatch(r"[0-9]{6}").all():
        raise ValueError("Period keys must remain opaque six-digit strings")
    mapper = calendar.set_index("rdate")["week_id"]
    daily["week_id"] = daily["date"].map(mapper)
    if daily["week_id"].isna().any():
        raise ValueError("Daily key missing from authoritative calendar")
    return {"calendar": calendar, "daily": daily, "weekly": weekly}


def _numeric(table: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    table = table.copy()
    for column in columns:
        raw = table[column].str.strip()
        blank = raw.str.lower().isin(["", "nan", "null", "none"])
        values = pd.to_numeric(raw.where(~blank), errors="coerce").to_numpy(dtype=float, na_value=np.nan)
        if ((~blank.to_numpy()) & ~np.isfinite(values)).any():
            raise ValueError(f"Malformed or nonfinite consumed value: {column}")
        table[column] = values
    return table


def _positions(request: dict, data: dict) -> tuple[int, int]:
    if request["daily_cutoff_key"] != request["feature_date"]:
        raise ValueError("Daily cutoff must equal the supplied weekly feature endpoint")
    d = data["daily"]
    w = data["weekly"]
    di = np.flatnonzero(d["date"].eq(request["daily_cutoff_key"]).to_numpy())
    wi = np.flatnonzero(w["week_id"].eq(request["weekly_cutoff_key"]).to_numpy())
    if len(di) != 1 or len(wi) != 1:
        raise ValueError("Consumed cutoff key absent or not unique")
    if d["week_id"].iloc[di[0]] != request["weekly_cutoff_key"]:
        raise ValueError("Daily cutoff does not map to supplied weekly cutoff")
    return int(di[0]), int(wi[0])


def _context(request: dict, data: dict) -> dict:
    di, wi = _positions(request, data)
    daily = _numeric(data["daily"].iloc[:di + 1], DAILY_COLUMNS)
    weekly = _numeric(data["weekly"].iloc[:wi + 1], WEEKLY_COLUMNS)
    if not np.isfinite(daily[TARGET].iloc[-1]):
        raise ValueError(f"Missing current daily target: code={TARGET} feature_date={request['feature_date']}")
    daily["date"] = pd.to_datetime(daily["date"], format="%Y-%m-%d", errors="raise")
    if (daily["date"].dt.dayofweek >= 5).any():
        raise ValueError("Weekend row is not a real trading observation")
    # 平台 Request 决定当前周端点；历史观测按绑定日历周键分组。
    # 周衍生收益率是否为空不构成交易日历，也不得据此丢掉合法周。
    # 日频真实收益率与其他周特征保持原样，不进行填充或跨截止读取。
    ep = daily.groupby("week_id", sort=False, observed=True).tail(1).sort_values("date").reset_index(drop=True)
    ep = ep.loc[ep[TARGET].notna()].reset_index(drop=True)
    if ep.empty or ep["date"].iloc[-1] != pd.Timestamp(request["feature_date"]):
        raise ValueError("Cutoff does not produce supplied feature endpoint")
    day = daily.set_index("date")
    delta = (ep[TARGET].shift(-1) - ep[TARGET]).to_numpy(float)
    target_dates = ep["date"].shift(-1)
    references, needs_union = {}, False
    def collect(node: dict) -> None:
        nonlocal needs_union
        if node["kind"] == "reference":
            cfg = node["config"]
            rid = cfg["scheme_id"]
            if rid not in references:
                ref_ep = ep[["date", "week_id", *cfg["daily_columns"]]]
                ref_weekly = weekly[["week_id", *cfg["weekly_columns"]]]
                references[rid] = _ref_features(ref_ep, ref_weekly, cfg["members"])
        elif node["kind"] == "logistic":
            needs_union = True
        else:
            for member in node["members"]:
                collect(member)
    collect(ALGORITHM)
    union = _union_features(day, ep) if needs_union else {}
    return {"ep": ep, "delta": delta, "target_dates": target_dates, "references": references, "union": union, "max_di": di, "max_wi": wi}


def _score_node(node: dict, context: dict, i: int, trace: list[float]) -> int:
    ep, delta, target_dates = context["ep"], context["delta"], context["target_dates"]
    train = np.flatnonzero(np.isfinite(delta) & (target_dates < ep["date"].iloc[i]).to_numpy())
    if node["kind"] == "reference":
        cfg = node["config"]
        eligible = train[delta[train] != 0]
        truth = np.sign(delta)
        matrix = context["references"][cfg["scheme_id"]].iloc[:i + 1]
        votes = []
        for member in cfg["members"]:
            probability = _ref_member_probability(member, matrix, truth, eligible)
            trace.append(float(probability))
            signal = 0 if abs(probability - .5) < member["reject"] else (1 if probability >= .5 else -1)
            votes.append(signal)
        if abs(sum(votes)) < cfg["quorum"]:
            raise ValueError("Frozen full-action quorum failed")
        return 1 if sum(votes) > 0 else -1
    if node["kind"] == "logistic":
        train = train[delta[train] != 0][-node["window"]:]
        if len(train) < 78:
            raise ValueError("Insufficient mature training history")
        matrix = context["union"][node["group"]]
        history, current = matrix.iloc[train], matrix.iloc[[i]]
        if history.notna().sum().min() < max(26, int(len(train) * .70)) or not np.isfinite(current.to_numpy()).all():
            raise ValueError("Insufficient training coverage or missing current features")
        yy = (delta[train] > 0).astype(int)
        if np.unique(yy).size != 2:
            raise ValueError("Training requires two classes")
        estimator = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                                  LogisticRegression(C=node["C"], solver="liblinear", max_iter=1000, random_state=NEW_RANDOM_STATE))
        estimator.fit(history, yy)
        score = float(estimator.predict_proba(current)[0, 1] - .5)
        if not math.isfinite(score):
            raise ValueError("Nonfinite model probability")
        trace.append(score)
        return 1 if score >= 0 else -1
    signals = [_score_node(member, context, i, trace) for member in node["members"]]
    if len(signals) != 3 or any(s not in (-1, 1) for s in signals):
        raise ValueError("Invalid frozen three-member ensemble")
    return 1 if sum(signals) > 0 else -1


def _score(request: dict, data: dict, context: dict) -> tuple[int, tuple[float, ...]]:
    di, wi = _positions(request, data)
    if di > context["max_di"] or wi > context["max_wi"]:
        raise ValueError("Request cutoff outside prepared prefix")
    ep = context["ep"]
    loc = np.flatnonzero(ep["date"].eq(pd.Timestamp(request["feature_date"])).to_numpy())
    if len(loc) != 1 or ep["week_id"].iloc[loc[0]] != request["weekly_cutoff_key"]:
        raise ValueError(f"Request cutoff has no exact completed endpoint: feature_date={request['feature_date']} week_id={request['weekly_cutoff_key']}")
    trace: list[float] = []
    with threadpool_limits(limits=1):
        direction = _score_node(ALGORITHM, context, int(loc[0]), trace)
    return int(direction), tuple(trace)


def generate(requests: Sequence[dict], data: dict) -> list[dict]:
    if not requests:
        raise ValueError("Empty request batch")
    requests = [validate_request(r) for r in requests]
    if len({r["request_id"] for r in requests}) != len(requests):
        raise ValueError("Duplicate request_id")
    positions = [_positions(r, data) for r in requests]
    last = max(range(len(requests)), key=lambda i: positions[i][0])
    if any(w > positions[last][1] for _, w in positions):
        raise ValueError("Daily/weekly cutoff prefix order inconsistent")
    context = _context(requests[last], data)
    rows, scores = [], []
    memo: dict[tuple, tuple] = {}
    for r in requests:
        key = (r["daily_cutoff_key"], r["weekly_cutoff_key"], r["monthly_cutoff_key"])
        if key not in memo:
            memo[key] = _score(r, data, context)
        direction, trace = memo[key]
        rows.append({**{k: r[k] for k in OUTPUT_FIELDS[:-1]}, "predicted_direction": direction})
        scores.append(trace)
    # All models still refit on their own strict mature windows. Only the causal
    # feature matrix is shared; check exact probabilities via separate prefixes.
    for i in sorted({0, len(requests) // 2, len(requests) - 1}):
        independent = _context(requests[i], data)
        direction, trace = _score(requests[i], data, independent)
        if direction != rows[i]["predicted_direction"] or trace != scores[i]:
            raise RuntimeError("Independent cutoff score/probability/direction check failed")
        digest = hashlib.sha256(json.dumps(trace, separators=(",", ":")).encode()).hexdigest()
        LOG.info("self_check request_id=%s score_sha256=%s fallback_used=false", requests[i]["request_id"], digest)
    return rows


def _validate_output(output: Path, data_dir: Path) -> None:
    if os.path.lexists(output) or not output.parent.is_dir():
        raise ValueError("Output must not exist and its parent must exist")
    resolved = output.parent.resolve() / output.name
    root = data_dir.resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError("Output must be outside read-only data-dir")


def _write(output: Path, content: str) -> None:
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.path.lexists(output):
            raise ValueError("Output appeared during computation")
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command, field in [("predict", "--request"), ("backtest", "--requests")]:
        sub = commands.add_parser(command)
        sub.add_argument(field, type=Path, required=True)
        sub.add_argument("--data-dir", type=Path, required=True)
        sub.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    warnings.filterwarnings("ignore", category=UserWarning)
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    try:
        _validate_output(args.output, args.data_dir)
        req = [read_request(args.request)] if args.command == "predict" else read_requests(args.requests)
        rows = generate(req, load_data(args.data_dir))
        if args.command == "predict":
            content = json.dumps(rows[0], ensure_ascii=False, separators=(",", ":")) + "\n"
        else:
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            content = stream.getvalue()
        _write(args.output, content)
        return 0
    except Exception as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
