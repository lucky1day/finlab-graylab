#!/usr/bin/env python3
"""Blackbox V2 weekly 1Y causal V1.31.0 curve-trust strategy.

The file is a self-contained Contract 1.0 delivery and supports exactly two
business commands: ``predict`` and ``backtest``.  It reads DataBridge V1
``weekly_output.csv`` and ``daily_output.csv`` plus the platform-provided
``api_wind_date.csv`` calendar.  Frozen center-model component code and all
fixed model parameters are embedded below; no network, database, model file,
project module, or cross-run state is used.
"""

from __future__ import annotations

import argparse
import csv
import contextlib
import json
import os
import re
import sys
import tempfile
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings(
    "ignore",
    category=UserWarning,
    message="Skipping features without any observed values.*",
)

import numpy as np
import pandas as pd


VERSION = "1.31.0"
STRATEGY_ID = "1Y-Causal-V1.31.0-Curve-Trust-Gray"
SCHEME_ID = "weekly_1y_causal_v1_31_0_standalone"
REQUEST_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "daily_cutoff_key",
    "weekly_cutoff_key",
    "monthly_cutoff_key",
)
RESULT_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
)
SPREAD_Z_WINDOW = 52
SPREAD_Z_MIN_PERIODS = 26
SPREAD_Z_THRESHOLD = -1.50
TRUST_EVENT_WINS = 2
REQUIRED_DATA_FILES = (
    "weekly_output.csv",
    "daily_output.csv",
    "api_wind_date.csv",
)
REQUIRED_WEEKLY_COLUMNS = (
    "week_id", "ATR1Y00W", "BIAS1Y0W", "G0100886", "G0100887",
    "G0100891", "HWC00003", "HWM00013", "HWM00015", "HWM00017",
    "HWM00018", "HWW00004", "HWW00006", "M0061614", "M0120188",
    "M0161675", "M0161676", "M0161677", "M0161684", "M0196870",
    "M0200612", "M0217126", "M0517126", "M1001795", "M1014902",
    "M1148909", "M2341115", "M2525763", "M5226731", "N0191645",
    "S0114089", "TB1YWI1H", "TB1YWI1L", "TB1YWI1V", "TB1YWI2V",
    "TB1YWI3C", "V0184553", "WC000001", "WC000004", "ZY000009",
    "ZY000010", "ZY000011", "ZY000071", "ZY000073", "ZY000075",
    "ZY000127",
)
REQUIRED_DAILY_COLUMNS = (
    "date", "90000002", "90000004", "90000007", "90000013",
    "90000014", "A4087688", "DR007IBC", "M0000271", "M0017142",
    "M0041371", "M0041372", "M0041739", "M0048486", "M1001646",
    "M1001794", "M1004263", "M1006336", "M1006337", "M1341115",
    "MACD1Y0D", "N0402001", "N0405001", "N1105001", "N1305001",
    "N2105001", "RSI1Y00D", "TB1YWI0C", "TB1YWI0V", "TB1YWI0W",
    "TB1YWIPC", "TCFE000C", "TCFE000I", "TCFE000V", "TSCFE001",
    "TSCFE002", "USDCNH0C", "X2879873",
)

_COMPONENT_ORDER = ('weekly_1y_causal_production_v1_6_2', 'explore_weekly_1y_causal_v1_11_0', 'explore_weekly_1y_causal_v1_11_0_market_ensemble', 'explore_weekly_1y_causal_v1_11_1_warmup_invariant', 'explore_weekly_1y_causal_v1_12_1_calibrated_blend', 'explore_weekly_1y_causal_v1_12_1_event_trust', 'explore_weekly_1y_causal_v1_12_1_incremental_towers', 'explore_weekly_1y_causal_v1_12_4_event_window_bagging', 'explore_weekly_1y_causal_v1_12_5_v111_confidence_veto', 'explore_weekly_1y_causal_v1_13_1_online_veto_trust', 'explore_weekly_1y_causal_v1_13_2_invariant_veto_bag', 'explore_weekly_1y_causal_v1_14_0_overlay_trust', 'explore_weekly_1y_causal_v1_20_0_magnitude_regression', 'explore_weekly_1y_causal_v1_12_2_funding_curve', 'explore_weekly_1y_causal_v1_14_6_event_chooser', 'explore_weekly_1y_causal_v1_25_0_target_state_event_router', 'explore_weekly_1y_causal_v1_25_1_invariant_daily_event_router', 'explore_weekly_1y_causal_v1_26_0_cross_expert_veto', 'explore_weekly_1y_causal_v1_27_0_orthogonal_sparse_union', 'weekly_1y_causal_production_v1_3_0')


_COMPONENT_BUILDERS: dict[str, Any] = {}


def _build_component_00(_components):
    """静态组件 weekly_1y_causal_production_v1_6_2 / pinned source component."""
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Any
    import numpy as np
    import pandas as pd
    from sklearn.impute import SimpleImputer
    from sklearn.feature_selection import SelectKBest
    from sklearn.feature_selection import f_classif
    from sklearn.linear_model import LogisticRegression
    from sklearn.linear_model import RidgeClassifier
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    TARGET_COL = 'TB1YWI3C'
    YIELD_COL = 'ZY000009'
    LABEL_THRESHOLD = 0.0
    MIN_TRAIN_WEEKS = 156
    MODEL_INCLUDED_MODULES = ('policy_liquidity', 'supply_institution', 'market_sentiment')
    ONLINE_WEIGHT_START_WEEK = 201901
    ONLINE_LOSS_WINDOW = 104
    ONLINE_MIN_HISTORY = 13
    ONLINE_WEIGHT_ETA = 50.0
    DECISION_THRESHOLD = 0.475
    PROBABILITY_SHRINK = 1.0
    MODEL_CONFIGS: tuple[dict[str, Any], ...] = ({'name': 'expanding_k24', 'window': None, 'k': 24, 'logistic_c': 0.2, 'ridge_alpha': 3.0}, {'name': 'rolling260_k20', 'window': 260, 'k': 20, 'logistic_c': 0.25, 'ridge_alpha': 3.0}, {'name': 'rolling208_k16', 'window': 208, 'k': 16, 'logistic_c': 0.2, 'ridge_alpha': 5.0}, {'name': 'rolling156_k12', 'window': 156, 'k': 12, 'logistic_c': 0.15, 'ridge_alpha': 8.0})

    @dataclass(frozen=True)
    class CausalSpec:
        module: str
        submodule: str
        code: str
        transform: str
        yield_sign: float
        preferred_agg: str = 'last'
        spread_against: str | None = None
        role: str = 'directional'
        variant: str = ''

        @property
        def feature_id(self) -> str:
            base = self.code if self.spread_against is None else f'{self.code}_minus_{self.spread_against}'
            if self.variant:
                base = f'{base}_{self.variant}'
            return f'{self.module}__{self.submodule}__{base}'

        @property
        def feature_column(self) -> str:
            suffix = 'yield_pressure' if self.role == 'directional' else 'state'
            return f'{self.feature_id}__{suffix}'
    CAUSAL_SPECS: tuple[CausalSpec, ...] = (CausalSpec('macro_fundamental', 'growth', 'M0217126', 'level_z', 1.0), CausalSpec('macro_fundamental', 'growth', 'M0517126', 'change_z', 1.0), CausalSpec('macro_fundamental', 'growth', 'M2525763', 'level_z', 1.0), CausalSpec('macro_fundamental', 'growth', 'M5226731', 'level_z', 1.0), CausalSpec('macro_fundamental', 'growth', 'M0161684', 'level_z', 1.0), CausalSpec('macro_fundamental', 'growth', 'M0161675', 'level_z', 1.0), CausalSpec('macro_fundamental', 'growth', 'V0184553', 'level_z', 1.0), CausalSpec('macro_fundamental', 'growth', 'S0114089', 'change_z', 1.0), CausalSpec('macro_fundamental', 'inflation', 'M0200612', 'level_z', 1.0), CausalSpec('macro_fundamental', 'inflation', 'M0161676', 'level_z', 1.0), CausalSpec('macro_fundamental', 'inflation', 'M0161677', 'level_z', 1.0), CausalSpec('macro_fundamental', 'inflation', 'HWM00017', 'pct_change_z', 1.0), CausalSpec('macro_fundamental', 'inflation', 'HWM00018', 'pct_change_z', 1.0), CausalSpec('macro_fundamental', 'inflation', 'HWM00015', 'pct_change_z', 1.0), CausalSpec('macro_fundamental', 'inflation', 'HWM00013', 'pct_change_z', 1.0), CausalSpec('policy_liquidity', 'policy', 'M0061614', 'change_z', -1.0), CausalSpec('policy_liquidity', 'policy', 'M0196870', 'level_z', 1.0), CausalSpec('policy_liquidity', 'policy', 'M0041371', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding', 'M1001795', 'level_z', 1.0), CausalSpec('policy_liquidity', 'funding', 'DR007IBC', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding', 'M1006337', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding', 'M0017142', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding', 'M1014902', 'level_z', 1.0), CausalSpec('policy_liquidity', 'funding', '90000002', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding', '90000014', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding_spread', '90000004', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding_spread', '90000013', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding_spread', 'ZY000009', 'spread_change_z', 1.0, spread_against='M1001795'), CausalSpec('policy_liquidity', 'funding_spread', 'ZY000009', 'spread_change_z', 1.0, spread_against='M1014902'), CausalSpec('policy_liquidity', 'front_curve', 'ZY000009', 'spread_change_z', 1.0, spread_against='ZY000071'), CausalSpec('supply_institution', 'supply_proxy', 'ZY000127', 'spread_change_z', 1.0, spread_against='ZY000009'), CausalSpec('supply_institution', 'institution', 'M1148909', 'change_z', -1.0), CausalSpec('supply_institution', 'institution', 'M2341115', 'change_z', -1.0), CausalSpec('supply_institution', 'trading_state', 'TB1YWI1V', 'change_z', 0.0, role='state'), CausalSpec('supply_institution', 'trading_state', 'TB1YWI2V', 'level_z', 0.0, role='state'), CausalSpec('supply_institution', 'trading_state', 'TB1YWI1H', 'spread_change_z', 0.0, spread_against='TB1YWI1L', role='state'), CausalSpec('supply_institution', 'funding_activity', 'M0041739', 'change_z', 0.0, 'mean', role='state'), CausalSpec('market_sentiment', 'risk_appetite', 'WC000004', 'pct_change_z', 1.0), CausalSpec('market_sentiment', 'risk_appetite', 'M0120188', 'pct_change_z', 1.0), CausalSpec('market_sentiment', 'risk_appetite', 'N0191645', 'pct_change_z', 1.0), CausalSpec('market_sentiment', 'credit', 'N1105001', 'spread_change_z', -1.0, 'mean', 'M1001646'), CausalSpec('market_sentiment', 'credit', 'N1305001', 'spread_change_z', -1.0, 'mean', 'M1001646'), CausalSpec('market_sentiment', 'credit', '90000007', 'change_z', -1.0, 'mean'), CausalSpec('market_sentiment', 'credit', 'M1004263', 'spread_change_z', -1.0, 'mean', 'M1001646'), CausalSpec('market_sentiment', 'bond_market', 'ZY000009', 'change_z', 1.0), CausalSpec('market_sentiment', 'curve', 'ZY000009', 'spread_change_z', 1.0, spread_against='ZY000010'), CausalSpec('market_sentiment', 'curve', 'ZY000009', 'spread_change_z', 1.0, spread_against='ZY000011'), CausalSpec('market_sentiment', 'technical', 'BIAS1Y0W', 'level_z', -1.0), CausalSpec('market_sentiment', 'technical_state', 'ATR1Y00W', 'level_z', 0.0, role='state'), CausalSpec('market_sentiment', 'technical_state', 'RSI1Y00D', 'level_z', 0.0, 'last', role='state'), CausalSpec('market_sentiment', 'technical_state', 'MACD1Y0D', 'level_z', 0.0, 'last', role='state'), CausalSpec('market_sentiment', 'price_path', 'TB1YWIPC', 'level_z', 0.0, 'mean', role='state'), CausalSpec('market_sentiment', 'price_path', 'TB1YWI0V', 'change_z', 0.0, 'mean', role='state'), CausalSpec('overseas_cross_asset', 'overseas_rates', 'G0100886', 'change_z', 1.0), CausalSpec('overseas_cross_asset', 'overseas_rates', 'G0100887', 'change_z', 1.0), CausalSpec('overseas_cross_asset', 'overseas_rates', 'G0100891', 'change_z', 1.0), CausalSpec('overseas_cross_asset', 'fx', 'HWC00003', 'change_z', 1.0), CausalSpec('overseas_cross_asset', 'fx', 'HWW00004', 'change_z', 1.0), CausalSpec('overseas_cross_asset', 'fx', 'M0000271', 'pct_change_z', 1.0), CausalSpec('overseas_cross_asset', 'commodity', 'WC000001', 'pct_change_z', -1.0), CausalSpec('overseas_cross_asset', 'commodity', 'HWM00013', 'pct_change_z', 1.0), CausalSpec('overseas_cross_asset', 'commodity', 'HWW00006', 'change_z', -1.0))

    def read_csv(path: Path) -> pd.DataFrame:
        if not path.exists():
            raise FileNotFoundError(f'Required input does not exist: {path}')
        frame = pd.read_csv(path, encoding='utf-8-sig')
        frame.columns = [column.strip().lstrip('\ufeff') for column in frame.columns]
        return frame

    def safe_numeric(series: pd.Series) -> pd.Series:
        return pd.to_numeric(series, errors='coerce').replace([np.inf, -np.inf], np.nan)

    def rolling_z(series: pd.Series, window: int=52, min_periods: int=12) -> pd.Series:
        values = safe_numeric(series)
        mean = values.rolling(window, min_periods=min_periods).mean()
        std = values.rolling(window, min_periods=min_periods).std()
        return values.sub(mean).div(std.replace(0, np.nan))

    def clipped(series: pd.Series, lower: float=-3.0, upper: float=3.0) -> pd.Series:
        return safe_numeric(series).clip(lower, upper)

    def validate_raw_inputs(weekly: pd.DataFrame, daily: pd.DataFrame, date_map: pd.DataFrame, metadata: pd.DataFrame) -> None:
        required_weekly = {'week_id', TARGET_COL, YIELD_COL}
        missing_weekly = sorted(required_weekly.difference(weekly.columns))
        if missing_weekly:
            raise ValueError(f'weekly_output.csv is missing required columns: {missing_weekly}')
        if weekly['week_id'].duplicated().any():
            duplicates = weekly.loc[weekly['week_id'].duplicated(), 'week_id'].head(10).tolist()
            raise ValueError(f'weekly_output.csv contains duplicate week_id values: {duplicates}')
        if 'date' not in daily.columns:
            raise ValueError('daily_output.csv is missing the date column')
        missing_date_map = sorted({'rdate', 'week_id'}.difference(date_map.columns))
        if missing_date_map:
            raise ValueError(f'api_wind_date.csv is missing required columns: {missing_date_map}')

    def prepare_daily(daily: pd.DataFrame, date_map: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        mapping = date_map[['rdate', 'week_id']].copy()
        mapping['rdate'] = pd.to_datetime(mapping['rdate'], errors='coerce')
        mapping['week_id'] = pd.to_numeric(mapping['week_id'], errors='coerce').astype('Int64')
        mapping = mapping.dropna(subset=['rdate', 'week_id']).drop_duplicates(['rdate', 'week_id'])
        out = daily.copy()
        out['date'] = pd.to_datetime(out['date'], errors='coerce')
        out = out.merge(mapping, left_on='date', right_on='rdate', how='left')
        out = out.drop(columns=['rdate'])
        unmatched_rows = int(out['week_id'].isna().sum())
        out = out[out['week_id'].notna()].copy()
        out['week_id'] = out['week_id'].astype(int)
        return (out, unmatched_rows)

    def aggregate_daily(daily: pd.DataFrame, code: str, aggregation: str) -> pd.Series:
        values = safe_numeric(daily[code])
        grouped = pd.DataFrame({'week_id': daily['week_id'], 'value': values})
        if aggregation == 'mean':
            return grouped.groupby('week_id')['value'].mean()
        if aggregation == 'sum':
            return grouped.groupby('week_id')['value'].sum()
        if aggregation == 'std':
            return grouped.groupby('week_id')['value'].std()
        return grouped.groupby('week_id')['value'].last()

    def resolve_series(weekly: pd.DataFrame, daily: pd.DataFrame, code: str, aggregation: str) -> tuple[pd.Series, str]:
        if code in weekly.columns:
            return (safe_numeric(weekly[code]), 'weekly')
        if code in daily.columns:
            aggregated = aggregate_daily(daily, code, aggregation)
            return (weekly['week_id'].map(aggregated).astype(float), f'daily_{aggregation}_to_week')
        return (pd.Series(np.nan, index=weekly.index, dtype=float), 'missing')

    def primary_score(raw: pd.Series, transform: str) -> pd.Series:
        if transform == 'level_z':
            return rolling_z(raw)
        if transform in {'change_z', 'spread_change_z'}:
            return rolling_z(raw.diff())
        if transform == 'pct_change_z':
            return rolling_z(raw.ffill().pct_change(fill_method=None))
        if transform == 'momentum4_z':
            return rolling_z(raw.diff(4))
        raise ValueError(f'Unknown causal transform: {transform}')

    def build_causal_features(weekly: pd.DataFrame, daily: pd.DataFrame) -> tuple[pd.DataFrame, list[str], dict[str, str]]:
        features = weekly[['week_id', TARGET_COL, YIELD_COL]].copy()
        feature_cols: list[str] = []
        sources: dict[str, str] = {}
        for spec in CAUSAL_SPECS:
            raw, source = resolve_series(weekly, daily, spec.code, spec.preferred_agg)
            if spec.spread_against:
                other, other_source = resolve_series(weekly, daily, spec.spread_against, spec.preferred_agg)
                raw = raw.sub(other)
                source = f'{source}_spread_against_{other_source}'
            feature_col = spec.feature_column
            transformed = primary_score(raw, spec.transform)
            features[feature_col] = clipped(transformed * spec.yield_sign if spec.role == 'directional' else transformed)
            feature_cols.append(feature_col)
            sources[feature_col] = source
        return (features, feature_cols, sources)

    def attach_calendar(frame: pd.DataFrame, date_map: pd.DataFrame) -> pd.DataFrame:
        required = {'rdate', 'week_id'}
        missing = sorted(required.difference(date_map.columns))
        if missing:
            raise ValueError(f'Date map is missing required columns: {missing}')
        mapped = date_map[['rdate', 'week_id']].copy()
        mapped['rdate'] = pd.to_datetime(mapped['rdate'], errors='coerce')
        mapped['week_id'] = pd.to_numeric(mapped['week_id'], errors='coerce').astype('Int64')
        mapped = mapped.dropna(subset=['rdate', 'week_id'])
        lookup = mapped.groupby('week_id')['rdate'].max()
        out = frame.copy()
        out['week_id'] = pd.to_numeric(out['week_id'], errors='raise').astype(int)
        out = out.sort_values('week_id').reset_index(drop=True)
        out['week_date'] = out['week_id'].map(lookup)
        if out['week_date'].isna().any():
            missing = out.loc[out['week_date'].isna(), 'week_id'].head(10).tolist()
            raise ValueError(f'api_wind_date.csv is missing week mappings: {missing}')
        out['week_date_source'] = 'api_wind_date'
        out['week_date'] = pd.to_datetime(out['week_date'])
        out['month'] = out['week_date'].dt.strftime('%Y-%m')
        return out

    def make_labels(frame: pd.DataFrame) -> pd.DataFrame:
        target = safe_numeric(frame[TARGET_COL])
        future_return = target.shift(-1).div(target).sub(1.0)
        actual = np.select([(future_return > LABEL_THRESHOLD).fillna(False).to_numpy(dtype=bool), (future_return < -LABEL_THRESHOLD).fillna(False).to_numpy(dtype=bool)], [1, -1], default=0).astype(float)
        actual[future_return.isna()] = np.nan
        yield_value = safe_numeric(frame[YIELD_COL])
        future_yield_change = yield_value.shift(-1).sub(yield_value)
        reference_yield_label = np.select([(future_yield_change > 0).fillna(False).to_numpy(dtype=bool), (future_yield_change < 0).fillna(False).to_numpy(dtype=bool)], [1, -1], default=0).astype(float)
        reference_yield_label[future_yield_change.isna()] = np.nan
        implied_bond_price_label = -reference_yield_label
        return pd.DataFrame({'week_id': frame['week_id'], 'week_date': frame['week_date'], 'week_date_source': frame['week_date_source'], 'month': frame['month'], TARGET_COL: target, YIELD_COL: yield_value, 'future_return': future_return, 'actual_label': actual, 'future_yield_change': future_yield_change, 'reference_yield_direction_label': reference_yield_label, 'implied_bond_price_direction_label': implied_bond_price_label})

    def make_model_pipeline(model_type: str, selected_k: int, config: dict[str, Any]) -> Pipeline:
        steps: list[tuple[str, Any]] = [('imputer', SimpleImputer(strategy='median')), ('scaler', StandardScaler())]
        if selected_k > 0:
            steps.append(('selector', SelectKBest(score_func=f_classif, k=selected_k)))
        if model_type == 'logistic':
            steps.append(('model', LogisticRegression(C=float(config['logistic_c']), class_weight='balanced', solver='liblinear', max_iter=1000, random_state=42)))
        else:
            steps.append(('model', RidgeClassifier(class_weight='balanced', alpha=float(config['ridge_alpha']))))
        return Pipeline(steps)

    def fit_ensemble_probabilities(X: pd.DataFrame, y_all: np.ndarray, train_index: np.ndarray, predict_index: np.ndarray, config: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
        x_train = X.iloc[train_index]
        x_predict = X.iloc[predict_index]
        available_columns = x_train.columns[x_train.notna().any(axis=0) & x_train.nunique(dropna=True).gt(1)]
        if len(available_columns) == 0:
            empty = np.full(len(predict_index), np.nan)
            return (empty, empty, empty, 0)
        x_train = x_train[available_columns]
        x_predict = x_predict[available_columns]
        y_train = (y_all[train_index] == 1).astype(int)
        selected_k = min(int(config['k']), len(available_columns))
        logistic_probability = np.full(len(predict_index), np.nan)
        ridge_probability = np.full(len(predict_index), np.nan)
        try:
            logistic = make_model_pipeline('logistic', selected_k, config)
            logistic.fit(x_train, y_train)
            logistic_probability = logistic.predict_proba(x_predict)[:, 1].astype(float)
        except Exception:
            pass
        try:
            ridge = make_model_pipeline('ridge', selected_k, config)
            ridge.fit(x_train, y_train)
            decision = np.asarray(ridge.decision_function(x_predict), dtype=float)
            ridge_probability = 1.0 / (1.0 + np.exp(-np.clip(decision, -35.0, 35.0)))
        except Exception:
            pass
        stacked = np.vstack([logistic_probability, ridge_probability])
        valid_count = np.isfinite(stacked).sum(axis=0)
        total = np.nansum(stacked, axis=0)
        ensemble = np.divide(total, valid_count, out=np.full(len(predict_index), np.nan), where=valid_count > 0)
        return (logistic_probability, ridge_probability, ensemble, selected_k)

    def trailing_component_weights(probability_history: list[np.ndarray], actual_history: list[float]) -> tuple[np.ndarray, np.ndarray, int]:
        component_count = len(MODEL_CONFIGS)
        equal_weights = np.full(component_count, 1.0 / component_count)
        if not probability_history:
            return (equal_weights, np.full(component_count, np.nan), 0)
        history_probability = np.asarray(probability_history[-ONLINE_LOSS_WINDOW:], dtype=float)
        history_actual = np.asarray(actual_history[-ONLINE_LOSS_WINDOW:], dtype=float)
        valid_actual = np.isfinite(history_actual)
        history_samples = int(valid_actual.sum())
        if history_samples < ONLINE_MIN_HISTORY:
            return (equal_weights, np.full(component_count, np.nan), history_samples)
        losses = np.full(component_count, np.nan)
        for component_index in range(component_count):
            component_probability = history_probability[:, component_index]
            valid = valid_actual & np.isfinite(component_probability)
            if valid.any():
                losses[component_index] = float(np.mean((component_probability[valid] - history_actual[valid]) ** 2))
        finite_loss = np.isfinite(losses)
        if not finite_loss.any():
            return (equal_weights, losses, history_samples)
        shifted_loss = losses - np.nanmin(losses)
        raw_weights = np.zeros(component_count)
        raw_weights[finite_loss] = np.exp(-ONLINE_WEIGHT_ETA * shifted_loss[finite_loss])
        if raw_weights.sum() <= 0:
            return (equal_weights, losses, history_samples)
        return (raw_weights / raw_weights.sum(), losses, history_samples)

    def finite_weighted_average(values: np.ndarray, weights: np.ndarray) -> float:
        valid = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
        if not valid.any():
            return np.nan
        normalized = weights[valid] / weights[valid].sum()
        return float(np.sum(values[valid] * normalized))

    def walk_forward_model(frame: pd.DataFrame, labels: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
        X = frame[feature_cols].apply(safe_numeric)
        y_all = labels['actual_label'].to_numpy()
        rows: list[dict[str, Any]] = []
        probability_history: list[np.ndarray] = []
        actual_history: list[float] = []
        for index, row in frame.iterrows():
            week_id = int(row['week_id'])
            if week_id < ONLINE_WEIGHT_START_WEEK:
                continue
            train_index = np.arange(index)
            train_index = train_index[~pd.isna(y_all[train_index])]
            train_index = train_index[y_all[train_index] != 0]
            if len(train_index) < MIN_TRAIN_WEEKS:
                continue
            if len(np.unique(y_all[train_index])) < 2:
                continue
            component_probability = np.full(len(MODEL_CONFIGS), np.nan)
            component_logistic = np.full(len(MODEL_CONFIGS), np.nan)
            component_ridge = np.full(len(MODEL_CONFIGS), np.nan)
            component_selected_k = np.zeros(len(MODEL_CONFIGS), dtype=int)
            component_train_samples = np.zeros(len(MODEL_CONFIGS), dtype=int)
            for component_index, config in enumerate(MODEL_CONFIGS):
                fit_index = train_index
                if config['window'] is not None:
                    fit_index = fit_index[-int(config['window']):]
                logistic, ridge, probability, selected_k = fit_ensemble_probabilities(X, y_all, fit_index, np.asarray([index]), config)
                component_logistic[component_index] = float(logistic[0])
                component_ridge[component_index] = float(ridge[0])
                component_probability[component_index] = float(probability[0])
                component_selected_k[component_index] = int(selected_k)
                component_train_samples[component_index] = int(len(fit_index))
            weights, trailing_losses, history_samples = trailing_component_weights(probability_history, actual_history)
            weights = np.where(np.isfinite(component_probability), weights, 0.0)
            if weights.sum() > 0:
                weights = weights / weights.sum()
            raw_probability = finite_weighted_average(component_probability, weights)
            if pd.isna(raw_probability):
                continue
            probability_up = float(np.clip(0.5 + PROBABILITY_SHRINK * (raw_probability - 0.5), 0.0, 1.0))
            threshold = DECISION_THRESHOLD
            output_row: dict[str, Any] = {'week_id': week_id, 'logistic_prob_up': finite_weighted_average(component_logistic, weights), 'ridge_prob_up': finite_weighted_average(component_ridge, weights), 'raw_prob_up': raw_probability, 'prob_up': probability_up, 'decision_threshold': threshold, 'probability_shrink': PROBABILITY_SHRINK, 'pred_label': 1 if probability_up >= threshold else -1, 'model_confidence': abs(probability_up - threshold), 'aggregation_method': 'trailing_brier_softmax', 'online_loss_window': ONLINE_LOSS_WINDOW, 'online_weight_eta': ONLINE_WEIGHT_ETA, 'online_history_samples': history_samples, 'component_probability_spread': float(np.nanstd(component_probability)), 'component_direction_agreement': int(max(np.sum(component_probability >= 0.5), np.sum(component_probability < 0.5))), 'model_train_samples_min': int(component_train_samples.min()), 'model_train_samples_max': int(component_train_samples.max()), 'model_train_end_week': int(frame.iloc[int(train_index[-1])]['week_id']), 'prediction_asof_week': week_id, 'model_input_feature_count': int(len(feature_cols)), 'model_feature_count_min': int(component_selected_k.min()), 'model_feature_count_max': int(component_selected_k.max())}
            for component_index, config in enumerate(MODEL_CONFIGS):
                name = str(config['name'])
                output_row[f'component_prob__{name}'] = component_probability[component_index]
                output_row[f'component_weight__{name}'] = weights[component_index]
                output_row[f'component_brier__{name}'] = trailing_losses[component_index]
            rows.append(output_row)
            actual_label = y_all[index]
            actual_binary = 1.0 if actual_label == 1 else 0.0 if actual_label == -1 else np.nan
            probability_history.append(component_probability.copy())
            actual_history.append(actual_binary)
        return pd.DataFrame(rows)
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['weekly_1y_causal_production_v1_6_2'] = _build_component_00


def _build_component_01(_components):
    """静态组件 explore_weekly_1y_causal_v1_11_0 / pinned source component."""
    from typing import Any
    import numpy as np
    import pandas as pd
    base = _components['weekly_1y_causal_production_v1_6_2']
    DECISION_THRESHOLD = 0.475
    DAILY_PATH_SPECS: tuple[tuple[str, str, tuple[str, ...], str | None], ...] = (('liquidity', 'DR007IBC', ('last', 'change', 'range', 'volatility'), None), ('liquidity', 'M0017142', ('last', 'change', 'range', 'volatility'), None), ('liquidity', '90000002', ('last', 'change', 'range'), None), ('liquidity', '90000004', ('last', 'change', 'range'), None), ('liquidity', '90000013', ('last', 'change', 'range'), None), ('liquidity', '90000014', ('last', 'change', 'range'), None), ('liquidity', 'M0041739', ('mean', 'change', 'volatility'), None), ('credit', '90000007', ('last', 'change', 'range'), None), ('credit', 'N1105001', ('last', 'change', 'range'), 'M1001646'), ('credit', 'N1305001', ('last', 'change', 'range'), 'M1001646'), ('credit', 'M1004263', ('last', 'change', 'range'), 'M1001646'), ('market_path', 'TB1YWI0C', ('change', 'range', 'volatility', 'slope'), None), ('market_path', 'TB1YWI0V', ('mean', 'change', 'volatility'), None), ('market_path', 'TB1YWI0W', ('mean', 'change'), None), ('market_path', 'TB1YWIPC', ('mean', 'last', 'volatility'), None), ('external', 'M0000271', ('last', 'change', 'range'), None), ('external', 'USDCNH0C', ('last', 'change', 'range'), None))

    def rolling_z(series: pd.Series, window: int=52, min_periods: int=26) -> pd.Series:
        values = base.safe_numeric(series)
        mean = values.rolling(window, min_periods=min_periods).mean()
        std = values.rolling(window, min_periods=min_periods).std()
        return values.sub(mean).div(std.replace(0.0, np.nan))

    def group_slope(values: pd.Series) -> float:
        numeric = base.safe_numeric(values).dropna()
        if len(numeric) < 2:
            return np.nan
        x = np.arange(len(numeric), dtype=float)
        return float(np.polyfit(x, numeric.to_numpy(dtype=float), 1)[0])

    def aggregate_week_path(daily: pd.DataFrame, raw: pd.Series, statistic: str) -> pd.Series:
        frame = pd.DataFrame({'week_id': daily['week_id'].to_numpy(), 'date': daily['date'].to_numpy(), 'value': base.safe_numeric(raw).to_numpy()}).sort_values(['week_id', 'date'])
        grouped = frame.groupby('week_id', sort=True)['value']
        if statistic == 'last':
            return grouped.last()
        if statistic == 'mean':
            return grouped.mean()
        if statistic == 'change':
            return grouped.last().sub(grouped.first())
        if statistic == 'range':
            return grouped.max().sub(grouped.min())
        if statistic == 'volatility':
            return grouped.std()
        if statistic == 'slope':
            return grouped.apply(group_slope)
        raise ValueError(f'Unsupported daily path statistic: {statistic}')

    def build_daily_week_features(weekly: pd.DataFrame, daily: pd.DataFrame) -> tuple[pd.DataFrame, list[str], pd.DataFrame]:
        out = weekly[['week_id']].copy()
        feature_columns: list[str] = []
        audit_rows: list[dict[str, Any]] = []
        recent = weekly['month'].astype(str).str[:4].isin(('2023', '2024'))
        for group, code, statistics, spread_against in DAILY_PATH_SPECS:
            if code not in daily.columns:
                raise ValueError(f'daily_output.csv is missing V1.11 code: {code}')
            raw = base.safe_numeric(daily[code])
            source_name = code
            if spread_against:
                if spread_against not in daily.columns:
                    raise ValueError(f'daily_output.csv is missing spread reference: {spread_against}')
                raw = raw.sub(base.safe_numeric(daily[spread_against]))
                source_name = f'{code}_minus_{spread_against}'
            valid_days = pd.DataFrame({'week_id': daily['week_id'], 'valid': raw.notna().astype(int)}).groupby('week_id')['valid'].sum()
            coverage_column = f'dailywk__{group}__{source_name}__valid_days'
            out[coverage_column] = weekly['week_id'].map(valid_days).fillna(0)
            for statistic in statistics:
                weekly_raw = aggregate_week_path(daily, raw, statistic)
                mapped = weekly['week_id'].map(weekly_raw).astype(float)
                feature_column = f'dailywk__{group}__{source_name}__{statistic}_z52'
                out[feature_column] = rolling_z(mapped).clip(-3.0, 3.0)
                feature_columns.append(feature_column)
                valid_weeks = weekly.loc[out[feature_column].notna(), 'week_id']
                audit_rows.append({'feature_column': feature_column, 'group': group, 'indicator_code': code, 'spread_against': spread_against or '', 'statistic': statistic, 'transform': 'rolling_z52', 'source': 'daily_output.csv', 'date_mapping': 'api_wind_date.csv', 'point_in_time_policy': 'mapped_prediction_week_observations_only', 'additional_weekly_lag': 0, 'coverage_all': float(out[feature_column].notna().mean()), 'coverage_2023_2024': float(out.loc[recent, feature_column].notna().mean()), 'mean_valid_days_2023_2024': float(out.loc[recent, coverage_column].mean()), 'first_valid_week': int(valid_weeks.min()) if not valid_weeks.empty else pd.NA, 'last_valid_week': int(valid_weeks.max()) if not valid_weeks.empty else pd.NA})
        return (out, feature_columns, pd.DataFrame(audit_rows))
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_11_0'] = _build_component_01


def _build_component_02(_components):
    """静态组件 explore_weekly_1y_causal_v1_11_0_market_ensemble / pinned source component."""
    from typing import Any
    import numpy as np
    import pandas as pd
    v11 = _components['explore_weekly_1y_causal_v1_11_0']
    base = _components['weekly_1y_causal_production_v1_6_2']

    def walk_forward_single_model(frame: pd.DataFrame, labels: pd.DataFrame, feature_columns: list[str], config: dict[str, Any]) -> pd.DataFrame:
        X = frame[feature_columns].apply(base.safe_numeric)
        y_all = labels['actual_label'].to_numpy()
        rows: list[dict[str, Any]] = []
        for index, row in frame.iterrows():
            week_id = int(row['week_id'])
            if week_id < base.ONLINE_WEIGHT_START_WEEK:
                continue
            train_index = np.arange(index)
            train_index = train_index[~pd.isna(y_all[train_index])]
            train_index = train_index[y_all[train_index] != 0]
            if len(train_index) < base.MIN_TRAIN_WEEKS:
                continue
            fit_index = train_index[-int(config['window']):]
            logistic, ridge, probability, selected_k = base.fit_ensemble_probabilities(X, y_all, fit_index, np.asarray([index]), config)
            if pd.isna(probability[0]):
                continue
            rows.append({'week_id': week_id, 'prob_up': float(probability[0]), 'pred_label': 1 if probability[0] >= v11.DECISION_THRESHOLD else -1, 'selected_k': int(selected_k), 'model_train_end_week': int(frame.iloc[int(train_index[-1])]['week_id']), 'prediction_asof_week': week_id})
        return pd.DataFrame(rows)
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_11_0_market_ensemble'] = _build_component_02


def _build_component_03(_components):
    """静态组件 explore_weekly_1y_causal_v1_11_1_warmup_invariant / pinned source component."""
    import numpy as np
    import pandas as pd
    v11 = _components['explore_weekly_1y_causal_v1_11_0']
    BASE_THRESHOLD = v11.DECISION_THRESHOLD

    def centered_margin_blend(base_model: pd.DataFrame, market_model: pd.DataFrame, market_weight: float, market_threshold: float, candidate_id: str) -> pd.DataFrame:
        out = base_model[['week_id', 'prob_up', 'pred_label']].rename(columns={'prob_up': 'base_prob_up', 'pred_label': 'base_pred_label'}).merge(market_model[['week_id', 'prob_up']].rename(columns={'prob_up': 'market_prob_up'}), on='week_id', how='inner', validate='one_to_one')
        out['combined_margin'] = (1.0 - market_weight) * (out['base_prob_up'] - BASE_THRESHOLD) + market_weight * (out['market_prob_up'] - market_threshold)
        out['prob_up'] = (1.0 - market_weight) * out['base_prob_up'] + market_weight * out['market_prob_up']
        out['pred_label'] = np.where(out['combined_margin'].ge(0.0), 1, -1)
        out['candidate_id'] = candidate_id
        return out
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_11_1_warmup_invariant'] = _build_component_03


def _build_component_04(_components):
    """静态组件 explore_weekly_1y_causal_v1_12_1_calibrated_blend / pinned source component."""
    import numpy as np
    import pandas as pd
    base = _components['weekly_1y_causal_production_v1_6_2']
    MODEL_WINDOWS = (182, 208, 234, 260)
    BLEND_PANELS: dict[str, tuple[str, ...]] = {'front': ('front_futures',), 'funding_front': ('funding_front',), 'duration': ('duration_positioning',), 'front_duration': ('front_futures', 'duration_positioning'), 'front_funding_duration': ('front_futures', 'funding_flow', 'duration_positioning')}

    def panel_probability(cache: pd.DataFrame, panel_id: str) -> pd.Series:
        columns = [f'prob_up__{panel_id}__window{window}' for window in MODEL_WINDOWS]
        return cache[columns].median(axis=1)

    def build_candidate(cache: pd.DataFrame, blend_id: str, tower_center: float, incremental_weight: float) -> pd.DataFrame:
        out = cache[['week_id', 'base_prob_up', 'base_pred_label']].dropna().copy()
        source = cache.loc[out.index]
        aligned_probabilities = []
        for panel_id in BLEND_PANELS[blend_id]:
            probability = panel_probability(source, panel_id)
            aligned = (base.DECISION_THRESHOLD + probability - tower_center).clip(0.0, 1.0)
            out[f'aligned_prob__{panel_id}'] = aligned.to_numpy()
            aligned_probabilities.append(aligned.to_numpy(dtype=float))
        incremental_probability = np.nanmean(np.column_stack(aligned_probabilities), axis=1)
        out['incremental_prob_up'] = incremental_probability
        out['prob_up'] = (1.0 - incremental_weight) * out['base_prob_up'].to_numpy(dtype=float) + incremental_weight * incremental_probability
        out['pred_label'] = np.where(out['prob_up'].ge(base.DECISION_THRESHOLD), 1, -1)
        out['changed_from_base'] = out['pred_label'].ne(out['base_pred_label'])
        out['candidate_id'] = f'{blend_id}__center{tower_center:.3f}__weight{incremental_weight:.2f}'
        return out
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_12_1_calibrated_blend'] = _build_component_04


def _build_component_05(_components):
    """静态组件 explore_weekly_1y_causal_v1_12_1_event_trust / pinned source component."""
    import numpy as np
    import pandas as pd
    PRIOR_STRENGTH = 2.0

    def apply_event_trust(raw: pd.DataFrame, evaluation: pd.DataFrame, event_window: int, trust_threshold: float) -> pd.DataFrame:
        out = raw.merge(evaluation[['week_id', 'actual_label']], on='week_id', how='left', validate='one_to_one').sort_values('week_id').reset_index(drop=True)
        event_history: list[bool] = []
        predictions = []
        probabilities = []
        trust_applied = []
        event_counts = []
        hit_rates = []
        smoothed_rates = []
        for _, row in out.iterrows():
            recent = event_history[-event_window:]
            event_count = len(recent)
            hits = int(sum(recent))
            hit_rate = hits / event_count if event_count else np.nan
            smoothed = (hits + 0.5 * PRIOR_STRENGTH) / (event_count + PRIOR_STRENGTH)
            raw_changed = bool(int(row['pred_label']) != int(row['base_pred_label']))
            trusted = bool(raw_changed and event_count >= event_window and (smoothed >= trust_threshold))
            predictions.append(int(row['pred_label']) if trusted else int(row['base_pred_label']))
            probabilities.append(float(row['prob_up']) if trusted else float(row['base_prob_up']))
            trust_applied.append(trusted)
            event_counts.append(event_count)
            hit_rates.append(hit_rate)
            smoothed_rates.append(smoothed)
            actual = row['actual_label']
            if raw_changed and actual in (-1.0, 1.0):
                event_history.append(int(row['pred_label']) == int(actual))
        out['raw_pred_label'] = out['pred_label']
        out['raw_prob_up'] = out['prob_up']
        out['pred_label'] = predictions
        out['prob_up'] = probabilities
        out['event_trust_applied'] = trust_applied
        out['trust_event_count'] = event_counts
        out['trust_hit_rate'] = hit_rates
        out['trust_smoothed_hit_rate'] = smoothed_rates
        return out
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_12_1_event_trust'] = _build_component_05


def _build_component_06(_components):
    """静态组件 explore_weekly_1y_causal_v1_12_1_incremental_towers / pinned source component."""
    from typing import Any
    import numpy as np
    import pandas as pd
    v11 = _components['explore_weekly_1y_causal_v1_11_0']
    base = _components['weekly_1y_causal_production_v1_6_2']
    DEVELOPMENT_YEARS = tuple((str(year) for year in range(2019, 2025)))
    MODEL_WINDOWS = (182, 208, 234, 260)

    def rolling_z(series: pd.Series, window: int=52, min_periods: int=26) -> pd.Series:
        return v11.rolling_z(series, window, min_periods).clip(-3.0, 3.0)

    def signed_log(series: pd.Series) -> pd.Series:
        values = base.safe_numeric(series)
        return np.sign(values) * np.log1p(values.abs())

    def weekly_statistic(daily: pd.DataFrame, code: str, statistic: str) -> pd.Series:
        values = base.safe_numeric(daily[code])
        frame = pd.DataFrame({'week_id': daily['week_id'], 'date': daily['date'], 'value': values}).sort_values(['week_id', 'date'])
        grouped = frame.groupby('week_id', sort=True)['value']
        if statistic == 'last':
            return grouped.last()
        if statistic == 'mean':
            return grouped.mean()
        if statistic == 'sum':
            return grouped.sum(min_count=1)
        if statistic == 'change':
            return grouped.last().sub(grouped.first())
        if statistic == 'range':
            return grouped.max().sub(grouped.min())
        if statistic == 'volatility':
            return grouped.std()
        raise ValueError(f'Unsupported statistic: {statistic}')

    def add_feature(frame: pd.DataFrame, weekly: pd.DataFrame, daily: pd.DataFrame, group: str, code: str, statistic: str, transform: str, audit_rows: list[dict[str, Any]]) -> str:
        raw_weekly = weekly_statistic(daily, code, statistic)
        mapped = weekly['week_id'].map(raw_weekly).astype(float)
        if transform == 'level_z':
            feature = rolling_z(mapped)
        elif transform == 'change_z':
            feature = rolling_z(mapped.diff())
        elif transform == 'log_level_z':
            feature = rolling_z(np.log1p(mapped.clip(lower=0.0)))
        elif transform == 'log_change_z':
            feature = rolling_z(np.log1p(mapped.clip(lower=0.0)).diff())
        elif transform == 'signed_log_z':
            feature = rolling_z(signed_log(mapped))
        else:
            raise ValueError(f'Unsupported transform: {transform}')
        column = f'incremental__{group}__{code}__{statistic}__{transform}'
        frame[column] = feature
        development = weekly['month'].astype(str).str[:4].isin(DEVELOPMENT_YEARS)
        valid_week = weekly.loc[feature.notna(), 'week_id']
        audit_rows.append({'feature_column': column, 'group': group, 'indicator_code': code, 'statistic': statistic, 'transform': transform, 'source': 'daily_output.csv', 'date_mapping': 'api_wind_date.csv', 'point_in_time_policy': 'prediction_week_observations_only', 'additional_weekly_lag': 0, 'coverage_2019_2024': float(feature.loc[development].notna().mean()), 'first_valid_week': int(valid_week.min()) if not valid_week.empty else pd.NA, 'last_valid_week': int(valid_week.max()) if not valid_week.empty else pd.NA})
        return column

    def build_incremental_features(weekly: pd.DataFrame, daily: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, list[str]], pd.DataFrame]:
        frame = weekly[['week_id']].copy()
        groups: dict[str, list[str]] = {'funding_flow': [], 'policy_quantity': [], 'front_futures': [], 'institution_event': [], 'duration_positioning': []}
        audit_rows: list[dict[str, Any]] = []
        specs = (('funding_flow', 'X2879873', 'mean', 'log_change_z'), ('funding_flow', 'X2879873', 'change', 'signed_log_z'), ('funding_flow', 'X2879873', 'volatility', 'log_level_z'), ('funding_flow', 'M0041739', 'mean', 'log_change_z'), ('funding_flow', 'M0041739', 'change', 'signed_log_z'), ('funding_flow', 'M0041739', 'volatility', 'log_level_z'), ('funding_flow', 'M1001794', 'change', 'level_z'), ('funding_flow', 'M1006336', 'change', 'level_z'), ('policy_quantity', 'A4087688', 'sum', 'signed_log_z'), ('policy_quantity', 'A4087688', 'sum', 'change_z'), ('policy_quantity', 'M0041372', 'sum', 'log_level_z'), ('policy_quantity', 'M0041372', 'sum', 'log_change_z'), ('front_futures', 'TSCFE001', 'sum', 'level_z'), ('front_futures', 'TSCFE001', 'volatility', 'level_z'), ('front_futures', 'TSCFE002', 'range', 'level_z'), ('front_futures', 'TSCFE002', 'volatility', 'level_z'), ('institution_event', 'M1341115', 'change', 'signed_log_z'), ('institution_event', 'M1341115', 'last', 'change_z'), ('duration_positioning', 'TCFE000I', 'last', 'log_change_z'), ('duration_positioning', 'TCFE000V', 'sum', 'log_change_z'), ('duration_positioning', 'TCFE000C', 'change', 'level_z'), ('duration_positioning', 'TCFE000C', 'range', 'level_z'))
        required = {code for _, code, _, _ in specs}
        missing = sorted(required.difference(daily.columns))
        if missing:
            raise ValueError(f'daily_output.csv is missing incremental fields: {missing}')
        for group, code, statistic, transform in specs:
            groups[group].append(add_feature(frame, weekly, daily, group, code, statistic, transform, audit_rows))
        rate_pressure = frame[[column for column in groups['funding_flow'] if '__M1001794__' in column or '__M1006336__' in column]].mean(axis=1)
        volume_pressure = frame[[column for column in groups['funding_flow'] if '__mean__log_change_z' in column]].mean(axis=1)
        interaction = (rate_pressure.clip(-2.0, 2.0) * volume_pressure.clip(-2.0, 2.0)).clip(-3.0, 3.0)
        interaction_column = 'incremental__funding_flow__rate_volume_pressure'
        frame[interaction_column] = interaction
        groups['funding_flow'].append(interaction_column)
        audit_rows.append({'feature_column': interaction_column, 'group': 'funding_flow', 'indicator_code': 'M1001794|M1006336|X2879873|M0041739', 'statistic': 'interaction', 'transform': 'clipped_product', 'source': 'daily_output.csv', 'date_mapping': 'api_wind_date.csv', 'point_in_time_policy': 'prediction_week_observations_only', 'additional_weekly_lag': 0, 'coverage_2019_2024': float(interaction.loc[weekly['month'].astype(str).str[:4].isin(DEVELOPMENT_YEARS)].notna().mean()), 'first_valid_week': int(weekly.loc[interaction.notna(), 'week_id'].min()), 'last_valid_week': int(weekly.loc[interaction.notna(), 'week_id'].max())})
        oi_change = next((column for column in groups['duration_positioning'] if '__TCFE000I__' in column))
        futures_change = next((column for column in groups['duration_positioning'] if '__TCFE000C__change__' in column))
        conviction_column = 'incremental__duration_positioning__price_oi_conviction'
        frame[conviction_column] = (frame[oi_change].clip(-2.0, 2.0) * frame[futures_change].clip(-2.0, 2.0)).clip(-3.0, 3.0)
        groups['duration_positioning'].append(conviction_column)
        audit_rows.append({'feature_column': conviction_column, 'group': 'duration_positioning', 'indicator_code': 'TCFE000I|TCFE000C', 'statistic': 'interaction', 'transform': 'clipped_product', 'source': 'daily_output.csv', 'date_mapping': 'api_wind_date.csv', 'point_in_time_policy': 'prediction_week_observations_only', 'additional_weekly_lag': 0, 'coverage_2019_2024': float(frame.loc[weekly['month'].astype(str).str[:4].isin(DEVELOPMENT_YEARS), conviction_column].notna().mean()), 'first_valid_week': int(weekly.loc[frame[conviction_column].notna(), 'week_id'].min()), 'last_valid_week': int(weekly.loc[frame[conviction_column].notna(), 'week_id'].max())})
        return (frame, groups, pd.DataFrame(audit_rows))
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_12_1_incremental_towers'] = _build_component_06


def _build_component_07(_components):
    """静态组件 explore_weekly_1y_causal_v1_12_4_event_window_bagging / pinned source component."""
    import numpy as np
    import pandas as pd

    def bag_predictions(variants: list[pd.DataFrame]) -> pd.DataFrame:
        merged: pd.DataFrame | None = None
        probability_columns = []
        prediction_columns = []
        for index, variant in enumerate(variants):
            probability_column = f'prob_up__variant{index}'
            prediction_column = f'pred_label__variant{index}'
            current = variant[['week_id', 'prob_up', 'pred_label']].rename(columns={'prob_up': probability_column, 'pred_label': prediction_column})
            merged = current if merged is None else merged.merge(current, on='week_id', how='inner', validate='one_to_one')
            probability_columns.append(probability_column)
            prediction_columns.append(prediction_column)
        if merged is None:
            raise ValueError('No event-window variants')
        base_columns = variants[0][['week_id', 'base_prob_up', 'base_pred_label']]
        merged = merged.merge(base_columns, on='week_id', how='left', validate='one_to_one')
        vote_sum = merged[prediction_columns].sum(axis=1)
        merged['prob_up'] = merged[probability_columns].mean(axis=1)
        merged['pred_label'] = np.where(vote_sum > 0, 1, np.where(vote_sum < 0, -1, merged['base_pred_label']))
        merged['variant_agreement_fraction'] = merged[prediction_columns].eq(merged['pred_label'], axis=0).mean(axis=1)
        return merged
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_12_4_event_window_bagging'] = _build_component_07


def _build_component_08(_components):
    """静态组件 explore_weekly_1y_causal_v1_12_5_v111_confidence_veto / pinned source component."""
    import numpy as np
    import pandas as pd
    blend = _components['explore_weekly_1y_causal_v1_12_1_calibrated_blend']
    event_trust = _components['explore_weekly_1y_causal_v1_12_1_event_trust']
    event_bag = _components['explore_weekly_1y_causal_v1_12_4_event_window_bagging']
    base = _components['weekly_1y_causal_production_v1_6_2']
    EVENT_WINDOWS = (7, 8, 9)
    TRUST_THRESHOLDS = (0.55, 0.575, 0.6)

    def funding_event_bag(flow_cache: pd.DataFrame, evaluation: pd.DataFrame, tower_center: float, incremental_weight: float) -> pd.DataFrame:
        raw = blend.build_candidate(flow_cache, 'funding_front', tower_center, incremental_weight)
        variants = []
        for event_window in EVENT_WINDOWS:
            for trust_threshold in TRUST_THRESHOLDS:
                variants.append(event_trust.apply_event_trust(raw, evaluation, event_window, trust_threshold))
        return event_bag.bag_predictions(variants)

    def apply_v111_veto(funding: pd.DataFrame, v111: pd.DataFrame, confidence_cap: float) -> pd.DataFrame:
        out = v111[['week_id', 'base_prob_up', 'base_pred_label', 'prob_up', 'pred_label']].rename(columns={'prob_up': 'v111_prob_up', 'pred_label': 'v111_pred_label'}).merge(funding[['week_id', 'prob_up', 'pred_label']].rename(columns={'prob_up': 'funding_prob_up', 'pred_label': 'funding_pred_label'}), on='week_id', how='inner', validate='one_to_one')
        v111_changed = out['v111_pred_label'].astype(int).ne(out['base_pred_label'].astype(int))
        funding_opposes = out['funding_pred_label'].astype(int).ne(out['v111_pred_label'].astype(int))
        v111_confidence = (out['v111_prob_up'].astype(float) - base.DECISION_THRESHOLD).abs()
        veto = v111_changed & funding_opposes & v111_confidence.le(confidence_cap)
        out['v111_confidence'] = v111_confidence
        out['funding_veto_applied'] = veto
        out['prob_up'] = np.where(veto, out['base_prob_up'], out['v111_prob_up'])
        out['pred_label'] = np.where(veto, out['base_pred_label'], out['v111_pred_label'])
        return out
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_12_5_v111_confidence_veto'] = _build_component_08


def _build_component_09(_components):
    """静态组件 explore_weekly_1y_causal_v1_13_1_online_veto_trust / pinned source component."""
    import pandas as pd
    EVENT_WINDOWS = (8, 12, 16)
    MINIMUM_EVENT_HISTORIES = (4, 6)
    POSTERIOR_THRESHOLDS = (0.5, 0.55)
    BETA_PRIOR_ALPHA = 2.0
    BETA_PRIOR_BETA = 2.0

    def apply_online_veto_trust(raw: pd.DataFrame, evaluation: pd.DataFrame, event_window: int, minimum_event_history: int, posterior_threshold: float) -> pd.DataFrame:
        out = raw.merge(evaluation[['week_id', 'actual_label']], on='week_id', how='left', validate='one_to_one').sort_values('week_id').reset_index(drop=True)
        event_outcomes: list[int] = []
        probabilities: list[float] = []
        predictions: list[int] = []
        applied_flags: list[bool] = []
        histories: list[int] = []
        posterior_means: list[float] = []
        for row in out.itertuples(index=False):
            proposal = bool(row.funding_veto_applied)
            recent = event_outcomes[-event_window:]
            history = len(recent)
            wins = sum((outcome == 1 for outcome in recent))
            posterior_mean = (wins + BETA_PRIOR_ALPHA) / (history + BETA_PRIOR_ALPHA + BETA_PRIOR_BETA)
            apply = bool(proposal and history >= minimum_event_history and (posterior_mean >= posterior_threshold))
            if apply:
                probability = float(row.prob_up)
                prediction = int(row.pred_label)
            else:
                probability = float(row.v111_prob_up)
                prediction = int(row.v111_pred_label)
            probabilities.append(probability)
            predictions.append(prediction)
            applied_flags.append(apply)
            histories.append(history)
            posterior_means.append(posterior_mean)
            if proposal and row.actual_label in (-1.0, 1.0):
                event_outcomes.append(1 if int(row.pred_label) == int(row.actual_label) else 0)
        result = out[['week_id', 'base_prob_up', 'base_pred_label', 'v111_prob_up', 'v111_pred_label', 'v111_confidence', 'funding_veto_applied']].copy()
        result['prior_event_history'] = histories
        result['prior_event_posterior_mean'] = posterior_means
        result['trusted_veto_applied'] = applied_flags
        result['prob_up'] = probabilities
        result['pred_label'] = predictions
        return result
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_13_1_online_veto_trust'] = _build_component_09


def _build_component_10(_components):
    """静态组件 explore_weekly_1y_causal_v1_13_2_invariant_veto_bag / pinned source component."""
    import numpy as np
    import pandas as pd
    trust = _components['explore_weekly_1y_causal_v1_13_1_online_veto_trust']
    FROZEN_TOWER_CENTER = 0.535
    FROZEN_INCREMENTAL_WEIGHT = 0.55
    FROZEN_CONFIDENCE_CAP = 0.025
    TRUST_HISTORY_START_WEEK_ID = 201909

    def bag_online_trust(raw: pd.DataFrame, evaluation: pd.DataFrame) -> pd.DataFrame:
        variants = []
        for event_window in trust.EVENT_WINDOWS:
            for minimum_history in trust.MINIMUM_EVENT_HISTORIES:
                for posterior_threshold in trust.POSTERIOR_THRESHOLDS:
                    variants.append(trust.apply_online_veto_trust(raw, evaluation, event_window, minimum_history, posterior_threshold))
        result = raw[['week_id', 'base_prob_up', 'base_pred_label', 'v111_prob_up', 'v111_pred_label', 'v111_confidence', 'funding_veto_applied', 'prob_up', 'pred_label']].rename(columns={'prob_up': 'raw_veto_prob_up', 'pred_label': 'raw_veto_pred_label'}).copy()
        probability_columns = []
        applied_columns = []
        for index, variant in enumerate(variants):
            current = variant[['week_id', 'prob_up', 'trusted_veto_applied']].rename(columns={'prob_up': f'variant_prob_up_{index}', 'trusted_veto_applied': f'variant_applied_{index}'})
            result = result.merge(current, on='week_id', how='inner', validate='one_to_one')
            probability_columns.append(f'variant_prob_up_{index}')
            applied_columns.append(f'variant_applied_{index}')
        result['variant_count'] = len(variants)
        result['trusted_veto_votes'] = result[applied_columns].astype(int).sum(axis=1)
        result['trusted_veto_applied'] = result['trusted_veto_votes'] > len(variants) / 2.0
        result['prob_up'] = result[probability_columns].mean(axis=1)
        result['pred_label'] = np.where(result['trusted_veto_applied'], result['raw_veto_pred_label'], result['v111_pred_label']).astype(int)
        return result.drop(columns=[*probability_columns, *applied_columns])
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_13_2_invariant_veto_bag'] = _build_component_10


def _build_component_11(_components):
    """静态组件 explore_weekly_1y_causal_v1_14_0_overlay_trust / pinned source component."""
    import numpy as np
    import pandas as pd

    def bag_variants(variants: list[pd.DataFrame], baseline: pd.DataFrame, candidate_id: str) -> pd.DataFrame:
        out = baseline[['week_id', 'base_prob_up', 'base_pred_label', 'v111_prob_up', 'v111_pred_label', 'prob_up', 'pred_label']].rename(columns={'prob_up': 'v13_prob_up', 'pred_label': 'v13_pred_label'}).copy()
        probability_columns = []
        prediction_columns = []
        for index, variant in enumerate(variants):
            probability_column = f'variant_prob_up_{index}'
            prediction_column = f'variant_pred_label_{index}'
            out = out.merge(variant[['week_id', 'prob_up', 'pred_label']].rename(columns={'prob_up': probability_column, 'pred_label': prediction_column}), on='week_id', how='inner', validate='one_to_one')
            probability_columns.append(probability_column)
            prediction_columns.append(prediction_column)
        out['prob_up'] = out[probability_columns].mean(axis=1)
        vote_sum = out[prediction_columns].sum(axis=1)
        out['pred_label'] = np.where(vote_sum > 0, 1, np.where(vote_sum < 0, -1, out['v13_pred_label'])).astype(int)
        out['candidate_id'] = candidate_id
        return out.drop(columns=[*probability_columns, *prediction_columns])
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_14_0_overlay_trust'] = _build_component_11


def _build_component_12(_components):
    """静态组件 explore_weekly_1y_causal_v1_20_0_magnitude_regression / pinned source component."""
    import numpy as np
    import pandas as pd
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    base = _components['weekly_1y_causal_production_v1_6_2']
    WINDOWS = (156, 208, 260)
    RIDGE_ALPHAS = (1.0, 10.0, 100.0)
    CONFIDENCE_THRESHOLDS = (0.1, 0.15, 0.18, 0.2, 0.22, 0.25, 0.28, 0.3, 0.35)

    def walk_forward_regression(features: pd.DataFrame, labels: pd.DataFrame, feature_columns: list[str], window: int, ridge_alpha: float, model_id: str) -> pd.DataFrame:
        X = features[feature_columns].apply(base.safe_numeric)
        target = labels['future_return'].astype(float).to_numpy()
        rows = []
        for index, row in features.iterrows():
            week_id = int(row['week_id'])
            if week_id < base.ONLINE_WEIGHT_START_WEEK:
                continue
            train_index = np.arange(index)
            valid = np.isfinite(target[train_index])
            train_index = train_index[valid]
            if len(train_index) < base.MIN_TRAIN_WEEKS:
                continue
            fit_index = train_index[-window:]
            y_train = target[fit_index]
            lower, upper = np.quantile(y_train, [0.02, 0.98])
            y_train = np.clip(y_train, lower, upper)
            model = Pipeline([('imputer', SimpleImputer(strategy='median')), ('scaler', StandardScaler()), ('ridge', Ridge(alpha=ridge_alpha))])
            model.fit(X.iloc[fit_index], y_train)
            prediction = float(model.predict(X.iloc[[index]])[0])
            target_scale = float(np.std(y_train))
            standardized_prediction = prediction / target_scale if target_scale > 0.0 else 0.0
            probability = float(1.0 / (1.0 + np.exp(-np.clip(standardized_prediction, -20.0, 20.0))))
            rows.append({'week_id': week_id, 'predicted_future_return': prediction, 'predicted_return_z': standardized_prediction, 'prob_up': probability, 'pred_label': 1.0 if prediction >= 0.0 else -1.0, 'model_id': model_id})
        return pd.DataFrame(rows)

    def regression_consensus(group_bags: dict[str, pd.DataFrame], v13: pd.DataFrame, minimum_confidence: float, candidate_id: str) -> pd.DataFrame:
        merged = v13[['week_id', 'base_prob_up', 'base_pred_label', 'prob_up', 'pred_label']].rename(columns={'prob_up': 'v13_prob_up', 'pred_label': 'v13_pred_label'})
        prediction_columns = []
        probability_columns = []
        for group_id, bag in group_bags.items():
            current = bag[['week_id', 'prob_up', 'pred_label']].rename(columns={'prob_up': f'prob_up__{group_id}', 'pred_label': f'pred_label__{group_id}'})
            merged = merged.merge(current, on='week_id', how='left', validate='one_to_one')
            prediction_columns.append(f'pred_label__{group_id}')
            probability_columns.append(f'prob_up__{group_id}')
        vote = merged[prediction_columns].sum(axis=1)
        challenger_prediction = np.where(vote > 0.0, 1.0, -1.0)
        challenger_probability = merged[probability_columns].mean(axis=1)
        confidence = merged[probability_columns].sub(0.5).abs().mul(2.0).mean(axis=1)
        proposal = vote.abs().eq(len(prediction_columns)) & confidence.ge(minimum_confidence) & pd.Series(challenger_prediction, index=merged.index).ne(merged['v13_pred_label'])
        merged['regression_override_applied'] = proposal
        merged['regression_confidence'] = confidence
        merged['prob_up'] = np.where(proposal, challenger_probability, merged['v13_prob_up'])
        merged['pred_label'] = np.where(proposal, challenger_prediction, merged['v13_pred_label'])
        merged['candidate_id'] = candidate_id
        return merged
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_20_0_magnitude_regression'] = _build_component_12


def _build_component_13(_components):
    """静态组件 explore_weekly_1y_causal_v1_12_2_funding_curve / pinned source component."""
    from typing import Any
    import numpy as np
    import pandas as pd
    base = _components['weekly_1y_causal_production_v1_6_2']
    DEVELOPMENT_YEARS = tuple((str(year) for year in range(2019, 2025)))

    def rolling_z(series: pd.Series, window: int=52, min_periods: int=26) -> pd.Series:
        values = base.safe_numeric(series)
        mean = values.rolling(window, min_periods=min_periods).mean()
        std = values.rolling(window, min_periods=min_periods).std()
        return values.sub(mean).div(std.replace(0.0, np.nan)).clip(-3.0, 3.0)

    def aggregate_raw(daily: pd.DataFrame, raw: pd.Series, statistic: str) -> pd.Series:
        frame = pd.DataFrame({'week_id': daily['week_id'], 'date': daily['date'], 'value': base.safe_numeric(raw)}).sort_values(['week_id', 'date'])
        grouped = frame.groupby('week_id', sort=True)['value']
        if statistic == 'last':
            return grouped.last()
        if statistic == 'change':
            return grouped.last().sub(grouped.first())
        if statistic == 'range':
            return grouped.max().sub(grouped.min())
        if statistic == 'volatility':
            return grouped.std()
        raise ValueError(f'Unsupported statistic: {statistic}')

    def build_funding_curve_features(weekly: pd.DataFrame, daily: pd.DataFrame) -> tuple[pd.DataFrame, list[str], pd.DataFrame]:
        required = {'N0402001', 'N0405001', 'N2105001', 'M0048486', 'M1001646'}
        missing = sorted(required.difference(daily.columns))
        if missing:
            raise ValueError(f'daily_output.csv is missing funding curve fields: {missing}')
        raw_series = {'aaa_1y': base.safe_numeric(daily['N0405001']), 'aaa_3m': base.safe_numeric(daily['N0402001']), 'aaa_curve_1y_3m': base.safe_numeric(daily['N0405001']) - base.safe_numeric(daily['N0402001']), 'aaa_1y_minus_treasury_1y': base.safe_numeric(daily['N0405001']) - base.safe_numeric(daily['M1001646']), 'aaa_minus_tier_1y': base.safe_numeric(daily['N2105001']) - base.safe_numeric(daily['N0405001']), 'fr007_swap_1y_minus_treasury_1y': base.safe_numeric(daily['M0048486']) - base.safe_numeric(daily['M1001646'])}
        specs = (('aaa_1y', 'change'), ('aaa_1y', 'range'), ('aaa_1y', 'volatility'), ('aaa_3m', 'change'), ('aaa_3m', 'range'), ('aaa_curve_1y_3m', 'last'), ('aaa_curve_1y_3m', 'change'), ('aaa_curve_1y_3m', 'range'), ('aaa_1y_minus_treasury_1y', 'last'), ('aaa_1y_minus_treasury_1y', 'change'), ('aaa_1y_minus_treasury_1y', 'range'), ('aaa_minus_tier_1y', 'last'), ('aaa_minus_tier_1y', 'change'), ('fr007_swap_1y_minus_treasury_1y', 'last'), ('fr007_swap_1y_minus_treasury_1y', 'change'), ('fr007_swap_1y_minus_treasury_1y', 'range'))
        out = weekly[['week_id']].copy()
        columns: list[str] = []
        audit_rows: list[dict[str, Any]] = []
        development = weekly['month'].astype(str).str[:4].isin(DEVELOPMENT_YEARS)
        for factor_id, statistic in specs:
            aggregated = aggregate_raw(daily, raw_series[factor_id], statistic)
            mapped = weekly['week_id'].map(aggregated)
            column = f'fundingcurve__{factor_id}__{statistic}__z52'
            out[column] = rolling_z(mapped)
            columns.append(column)
            valid_week = weekly.loc[out[column].notna(), 'week_id']
            audit_rows.append({'feature_column': column, 'factor_id': factor_id, 'statistic': statistic, 'transform': 'rolling_z52', 'source': 'daily_output.csv', 'date_mapping': 'api_wind_date.csv', 'point_in_time_policy': 'prediction_week_observations_only', 'additional_weekly_lag': 0, 'coverage_2019_2024': float(out.loc[development, column].notna().mean()), 'first_valid_week': int(valid_week.min()), 'last_valid_week': int(valid_week.max())})
        return (out, columns, pd.DataFrame(audit_rows))
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_12_2_funding_curve'] = _build_component_13


def _build_component_14(_components):
    """静态组件 explore_weekly_1y_causal_v1_14_6_event_chooser / pinned source component."""
    from typing import Any
    import pandas as pd
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    MINIMUM_EVENTS = 24

    def make_model(logistic_c: float) -> Pipeline:
        return Pipeline([('imputer', SimpleImputer(strategy='median')), ('scaler', StandardScaler()), ('model', LogisticRegression(C=logistic_c, class_weight='balanced', solver='liblinear', max_iter=1000, random_state=42))])

    def walk_forward_choose(panel: pd.DataFrame, feature_columns: tuple[str, ...], event_window: int | None, logistic_c: float, candidate_id: str) -> pd.DataFrame:
        X = panel[list(feature_columns)].astype(float)
        valid_event_indices: list[int] = []
        rows: list[dict[str, Any]] = []
        for index, row in panel.iterrows():
            disagreement = bool(row['disagreement'])
            train_indices = [event_index for event_index in valid_event_indices if panel.loc[event_index, 'v13_correct'] in (0.0, 1.0)]
            if event_window is not None:
                train_indices = train_indices[-event_window:]
            if disagreement and len(train_indices) >= MINIMUM_EVENTS and (panel.loc[train_indices, 'v13_correct'].nunique() >= 2):
                model = make_model(logistic_c)
                model.fit(X.iloc[train_indices], panel.loc[train_indices, 'v13_correct'].astype(int))
                keep_probability = float(model.predict_proba(X.iloc[[index]])[0, 1])
                keep_v13 = keep_probability >= 0.5
                source = 'event_chooser'
            else:
                keep_probability = 1.0
                keep_v13 = True
                source = 'v13_fallback'
            prediction = int(row['v13_pred_label']) if keep_v13 or not disagreement else int(row['base_pred_label'])
            probability = float(row['v13_prob_up']) if keep_v13 or not disagreement else float(row['base_prob_up'])
            rows.append({'week_id': int(row['week_id']), 'base_prob_up': float(row['base_prob_up']), 'base_pred_label': int(row['base_pred_label']), 'v111_prob_up': float(row['v111_prob_up']), 'v111_pred_label': int(row['v111_pred_label']), 'v13_prob_up': float(row['v13_prob_up']), 'v13_pred_label': int(row['v13_pred_label']), 'disagreement': disagreement, 'prior_event_samples': len(train_indices), 'keep_v13_probability': keep_probability, 'chooser_source': source, 'prob_up': probability, 'pred_label': prediction})
            if disagreement and row['v13_correct'] in (0.0, 1.0):
                valid_event_indices.append(index)
        result = pd.DataFrame(rows)
        result['candidate_id'] = candidate_id
        return result
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_14_6_event_chooser'] = _build_component_14


def _build_component_15(_components):
    """静态组件 explore_weekly_1y_causal_v1_25_0_target_state_event_router / pinned source component."""
    MODEL_COLUMNS = ('base_abs_margin', 'v13_abs_margin', 'v13_direction', 'market_margin', 'funding_front_margin', 'funding_curve_margin', 'duration_margin')
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_25_0_target_state_event_router'] = _build_component_15


def _build_component_16(_components):
    """静态组件 explore_weekly_1y_causal_v1_25_1_invariant_daily_event_router / pinned source component."""
    EVENT_WINDOWS = (32, 36, 40, 44, 48, 52)
    LOGISTIC_CS = (0.03, 0.1, 0.3)
    STRATEGY_ID = '1Y-Causal-V1.25.1-Invariant-Daily-Event-Router'
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_25_1_invariant_daily_event_router'] = _build_component_16


def _build_component_17(_components):
    """静态组件 explore_weekly_1y_causal_v1_26_0_cross_expert_veto / pinned source component."""
    import numpy as np
    import pandas as pd

    def gate_router_with_expert(router_predictions: pd.DataFrame, expert_predictions: pd.DataFrame, expert_id: str) -> pd.DataFrame:
        out = router_predictions.merge(expert_predictions[['week_id', 'prob_up', 'pred_label']].rename(columns={'prob_up': 'expert_prob_up', 'pred_label': 'expert_pred_label'}), on='week_id', how='inner', validate='one_to_one')
        router_proposal = out['pred_label'].astype(int).ne(out['v13_pred_label'].astype(int))
        expert_confirms = out['expert_pred_label'].astype(int).eq(out['pred_label'].astype(int))
        applied = router_proposal & expert_confirms
        out['cross_expert_veto_applied'] = applied
        out['prob_up'] = np.where(applied, out['prob_up'], out['v13_prob_up'])
        out['pred_label'] = np.where(applied, out['pred_label'], out['v13_pred_label']).astype(int)
        out['candidate_id'] = f'router_gate__{expert_id}'
        return out
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_26_0_cross_expert_veto'] = _build_component_17


def _build_component_18(_components):
    """静态组件 explore_weekly_1y_causal_v1_27_0_orthogonal_sparse_union / pinned source component."""
    import numpy as np
    import pandas as pd

    def combine_sparse_overlays(v13: pd.DataFrame, magnitude_predictions: pd.DataFrame, router_predictions: pd.DataFrame, candidate_id: str) -> pd.DataFrame:
        out = v13[['week_id', 'base_prob_up', 'base_pred_label', 'v111_prob_up', 'v111_pred_label', 'prob_up', 'pred_label']].rename(columns={'prob_up': 'v13_prob_up', 'pred_label': 'v13_pred_label'}).merge(magnitude_predictions[['week_id', 'prob_up', 'pred_label']].rename(columns={'prob_up': 'magnitude_prob_up', 'pred_label': 'magnitude_pred_label'}), on='week_id', how='inner', validate='one_to_one').merge(router_predictions[['week_id', 'prob_up', 'pred_label']].rename(columns={'prob_up': 'router_prob_up', 'pred_label': 'router_pred_label'}), on='week_id', how='inner', validate='one_to_one')
        magnitude_applied = out['magnitude_pred_label'].astype(int).ne(out['v13_pred_label'].astype(int))
        router_applied = out['router_pred_label'].astype(int).ne(out['v13_pred_label'].astype(int))
        conflict = magnitude_applied & router_applied & out['magnitude_pred_label'].astype(int).ne(out['router_pred_label'].astype(int))
        proposal_count = magnitude_applied.astype(int) + router_applied.astype(int)
        probability_total = out['magnitude_prob_up'].where(magnitude_applied, 0.0) + out['router_prob_up'].where(router_applied, 0.0)
        proposal_probability = probability_total.div(proposal_count.replace(0, np.nan))
        applied = proposal_count.gt(0) & ~conflict
        proposed_direction = np.where(magnitude_applied, out['magnitude_pred_label'], out['router_pred_label'])
        out['magnitude_overlay_applied'] = magnitude_applied
        out['unanimous_router_applied'] = router_applied
        out['overlay_conflict'] = conflict
        out['prob_up'] = np.where(applied, proposal_probability, out['v13_prob_up'])
        out['pred_label'] = np.where(applied, proposed_direction, out['v13_pred_label']).astype(int)
        out['candidate_id'] = candidate_id
        return out
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['explore_weekly_1y_causal_v1_27_0_orthogonal_sparse_union'] = _build_component_18


def _build_component_19(_components):
    """静态组件 weekly_1y_causal_production_v1_3_0 / pinned source component."""
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Any
    import numpy as np
    import pandas as pd
    from sklearn.impute import SimpleImputer
    from sklearn.feature_selection import SelectKBest
    from sklearn.feature_selection import f_classif
    from sklearn.linear_model import LogisticRegression
    from sklearn.linear_model import RidgeClassifier
    from sklearn.metrics import balanced_accuracy_score
    from sklearn.metrics import brier_score_loss
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    VERSION = '1.3.0'
    STRATEGY_ID = f'1Y-Causal-V{VERSION}'
    TARGET_COL = 'TB1YWI3C'
    YIELD_COL = 'ZY000009'
    LABEL_THRESHOLD = 0.0
    MIN_TRAIN_WEEKS = 156
    MODEL_START_WEEK = 201501
    MODULE_ORDER = ('macro_fundamental', 'policy_liquidity', 'supply_institution', 'market_sentiment', 'overseas_cross_asset')
    MODEL_INCLUDED_MODULES = ('policy_liquidity', 'supply_institution', 'market_sentiment')
    ADAPTIVE_VALIDATION_WEEKS = 52
    ADAPTIVE_REFRESH_WEEKS = 4
    ADAPTIVE_THRESHOLDS = (0.45, 0.5, 0.55)
    PROBABILITY_SHRINKS = (0.5, 0.75, 1.0)
    MODEL_CONFIGS: tuple[dict[str, Any], ...] = ({'name': 'expanding_k24', 'window': None, 'k': 24, 'logistic_c': 0.2, 'ridge_alpha': 3.0}, {'name': 'rolling260_k20', 'window': 260, 'k': 20, 'logistic_c': 0.25, 'ridge_alpha': 3.0}, {'name': 'rolling208_k16', 'window': 208, 'k': 16, 'logistic_c': 0.2, 'ridge_alpha': 5.0}, {'name': 'rolling156_k12', 'window': 156, 'k': 12, 'logistic_c': 0.15, 'ridge_alpha': 8.0})
    MODULE_CN = {'macro_fundamental': '宏观基本面', 'policy_liquidity': '货币政策与流动性', 'supply_institution': '债券供给与机构行为', 'market_sentiment': '市场情绪与市场状态', 'overseas_cross_asset': '海外与大类资产联动'}

    @dataclass(frozen=True)
    class CausalSpec:
        module: str
        submodule: str
        code: str
        transform: str
        yield_sign: float
        preferred_agg: str = 'last'
        spread_against: str | None = None
        role: str = 'directional'
        variant: str = ''

        @property
        def feature_id(self) -> str:
            base = self.code if self.spread_against is None else f'{self.code}_minus_{self.spread_against}'
            if self.variant:
                base = f'{base}_{self.variant}'
            return f'{self.module}__{self.submodule}__{base}'

        @property
        def feature_column(self) -> str:
            suffix = 'yield_pressure' if self.role == 'directional' else 'state'
            return f'{self.feature_id}__{suffix}'
    CAUSAL_SPECS: tuple[CausalSpec, ...] = (CausalSpec('macro_fundamental', 'growth', 'M0217126', 'level_z', 1.0), CausalSpec('macro_fundamental', 'growth', 'M0517126', 'change_z', 1.0), CausalSpec('macro_fundamental', 'growth', 'M2525763', 'level_z', 1.0), CausalSpec('macro_fundamental', 'growth', 'M5226731', 'level_z', 1.0), CausalSpec('macro_fundamental', 'growth', 'M0161684', 'level_z', 1.0), CausalSpec('macro_fundamental', 'growth', 'M0161675', 'level_z', 1.0), CausalSpec('macro_fundamental', 'growth', 'V0184553', 'level_z', 1.0), CausalSpec('macro_fundamental', 'growth', 'S0114089', 'change_z', 1.0), CausalSpec('macro_fundamental', 'inflation', 'M0200612', 'level_z', 1.0), CausalSpec('macro_fundamental', 'inflation', 'M0161676', 'level_z', 1.0), CausalSpec('macro_fundamental', 'inflation', 'M0161677', 'level_z', 1.0), CausalSpec('macro_fundamental', 'inflation', 'HWM00017', 'pct_change_z', 1.0), CausalSpec('macro_fundamental', 'inflation', 'HWM00018', 'pct_change_z', 1.0), CausalSpec('macro_fundamental', 'inflation', 'HWM00015', 'pct_change_z', 1.0), CausalSpec('macro_fundamental', 'inflation', 'HWM00013', 'pct_change_z', 1.0), CausalSpec('policy_liquidity', 'policy', 'M0061614', 'change_z', -1.0), CausalSpec('policy_liquidity', 'policy', 'M0196870', 'level_z', 1.0), CausalSpec('policy_liquidity', 'policy', 'M0041371', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding', 'M1001795', 'level_z', 1.0), CausalSpec('policy_liquidity', 'funding', 'DR007IBC', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding', 'M1006337', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding', 'M0017142', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding', 'M1014902', 'level_z', 1.0), CausalSpec('policy_liquidity', 'funding', '90000002', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding', '90000014', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding_spread', '90000004', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding_spread', '90000013', 'level_z', 1.0, 'mean'), CausalSpec('policy_liquidity', 'funding_spread', 'ZY000009', 'spread_change_z', 1.0, spread_against='M1001795'), CausalSpec('policy_liquidity', 'funding_spread', 'ZY000009', 'spread_change_z', 1.0, spread_against='M1014902'), CausalSpec('policy_liquidity', 'front_curve', 'ZY000009', 'spread_change_z', 1.0, spread_against='ZY000071'), CausalSpec('supply_institution', 'supply_proxy', 'ZY000127', 'spread_change_z', 1.0, spread_against='ZY000009'), CausalSpec('supply_institution', 'institution', 'M1148909', 'change_z', -1.0), CausalSpec('supply_institution', 'institution', 'M2341115', 'change_z', -1.0), CausalSpec('supply_institution', 'trading_state', 'TB1YWI1V', 'change_z', 0.0, role='state'), CausalSpec('supply_institution', 'trading_state', 'TB1YWI2V', 'level_z', 0.0, role='state'), CausalSpec('supply_institution', 'trading_state', 'TB1YWI1H', 'spread_change_z', 0.0, spread_against='TB1YWI1L', role='state'), CausalSpec('supply_institution', 'funding_activity', 'M0041739', 'change_z', 0.0, 'mean', role='state'), CausalSpec('market_sentiment', 'risk_appetite', 'WC000004', 'pct_change_z', 1.0), CausalSpec('market_sentiment', 'risk_appetite', 'M0120188', 'pct_change_z', 1.0), CausalSpec('market_sentiment', 'risk_appetite', 'N0191645', 'pct_change_z', 1.0), CausalSpec('market_sentiment', 'credit', 'N1105001', 'spread_change_z', -1.0, 'mean', 'M1001646'), CausalSpec('market_sentiment', 'credit', 'N1305001', 'spread_change_z', -1.0, 'mean', 'M1001646'), CausalSpec('market_sentiment', 'credit', '90000007', 'change_z', -1.0, 'mean'), CausalSpec('market_sentiment', 'credit', 'M1004263', 'spread_change_z', -1.0, 'mean', 'M1001646'), CausalSpec('market_sentiment', 'bond_market', 'ZY000009', 'change_z', 1.0), CausalSpec('market_sentiment', 'curve', 'ZY000009', 'spread_change_z', 1.0, spread_against='ZY000010'), CausalSpec('market_sentiment', 'curve', 'ZY000009', 'spread_change_z', 1.0, spread_against='ZY000011'), CausalSpec('market_sentiment', 'technical', 'BIAS1Y0W', 'level_z', -1.0), CausalSpec('market_sentiment', 'technical_state', 'ATR1Y00W', 'level_z', 0.0, role='state'), CausalSpec('market_sentiment', 'technical_state', 'RSI1Y00D', 'level_z', 0.0, 'last', role='state'), CausalSpec('market_sentiment', 'technical_state', 'MACD1Y0D', 'level_z', 0.0, 'last', role='state'), CausalSpec('market_sentiment', 'price_path', 'TB1YWIPC', 'level_z', 0.0, 'mean', role='state'), CausalSpec('market_sentiment', 'price_path', 'TB1YWI0V', 'change_z', 0.0, 'mean', role='state'), CausalSpec('overseas_cross_asset', 'overseas_rates', 'G0100886', 'change_z', 1.0), CausalSpec('overseas_cross_asset', 'overseas_rates', 'G0100887', 'change_z', 1.0), CausalSpec('overseas_cross_asset', 'overseas_rates', 'G0100891', 'change_z', 1.0), CausalSpec('overseas_cross_asset', 'fx', 'HWC00003', 'change_z', 1.0), CausalSpec('overseas_cross_asset', 'fx', 'HWW00004', 'change_z', 1.0), CausalSpec('overseas_cross_asset', 'fx', 'M0000271', 'pct_change_z', 1.0), CausalSpec('overseas_cross_asset', 'commodity', 'WC000001', 'pct_change_z', -1.0), CausalSpec('overseas_cross_asset', 'commodity', 'HWM00013', 'pct_change_z', 1.0), CausalSpec('overseas_cross_asset', 'commodity', 'HWW00006', 'change_z', -1.0))

    def read_csv(path: Path) -> pd.DataFrame:
        if not path.exists():
            raise FileNotFoundError(f'Required input does not exist: {path}')
        frame = pd.read_csv(path, encoding='utf-8-sig')
        frame.columns = [column.strip().lstrip('\ufeff') for column in frame.columns]
        return frame

    def safe_numeric(series: pd.Series) -> pd.Series:
        return pd.to_numeric(series, errors='coerce').replace([np.inf, -np.inf], np.nan)

    def rolling_z(series: pd.Series, window: int=52, min_periods: int=12) -> pd.Series:
        values = safe_numeric(series)
        mean = values.rolling(window, min_periods=min_periods).mean()
        std = values.rolling(window, min_periods=min_periods).std()
        return values.sub(mean).div(std.replace(0, np.nan))

    def clipped(series: pd.Series, lower: float=-3.0, upper: float=3.0) -> pd.Series:
        return safe_numeric(series).clip(lower, upper)

    def validate_raw_inputs(weekly: pd.DataFrame, daily: pd.DataFrame, date_map: pd.DataFrame, metadata: pd.DataFrame) -> None:
        required_weekly = {'week_id', TARGET_COL, YIELD_COL}
        missing_weekly = sorted(required_weekly.difference(weekly.columns))
        if missing_weekly:
            raise ValueError(f'weekly_output.csv is missing required columns: {missing_weekly}')
        if weekly['week_id'].duplicated().any():
            duplicates = weekly.loc[weekly['week_id'].duplicated(), 'week_id'].head(10).tolist()
            raise ValueError(f'weekly_output.csv contains duplicate week_id values: {duplicates}')
        if 'date' not in daily.columns:
            raise ValueError('daily_output.csv is missing the date column')
        missing_date_map = sorted({'rdate', 'week_id'}.difference(date_map.columns))
        if missing_date_map:
            raise ValueError(f'api_wind_date.csv is missing required columns: {missing_date_map}')

    def prepare_daily(daily: pd.DataFrame, date_map: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        mapping = date_map[['rdate', 'week_id']].copy()
        mapping['rdate'] = pd.to_datetime(mapping['rdate'], errors='coerce')
        mapping['week_id'] = pd.to_numeric(mapping['week_id'], errors='coerce').astype('Int64')
        mapping = mapping.dropna(subset=['rdate', 'week_id']).drop_duplicates(['rdate', 'week_id'])
        out = daily.copy()
        out['date'] = pd.to_datetime(out['date'], errors='coerce')
        out = out.merge(mapping, left_on='date', right_on='rdate', how='left')
        out = out.drop(columns=['rdate'])
        unmatched_rows = int(out['week_id'].isna().sum())
        out = out[out['week_id'].notna()].copy()
        out['week_id'] = out['week_id'].astype(int)
        return (out, unmatched_rows)

    def aggregate_daily(daily: pd.DataFrame, code: str, aggregation: str) -> pd.Series:
        values = safe_numeric(daily[code])
        grouped = pd.DataFrame({'week_id': daily['week_id'], 'value': values})
        if aggregation == 'mean':
            return grouped.groupby('week_id')['value'].mean()
        if aggregation == 'sum':
            return grouped.groupby('week_id')['value'].sum()
        if aggregation == 'std':
            return grouped.groupby('week_id')['value'].std()
        return grouped.groupby('week_id')['value'].last()

    def resolve_series(weekly: pd.DataFrame, daily: pd.DataFrame, code: str, aggregation: str) -> tuple[pd.Series, str]:
        if code in weekly.columns:
            return (safe_numeric(weekly[code]), 'weekly')
        if code in daily.columns:
            aggregated = aggregate_daily(daily, code, aggregation)
            return (weekly['week_id'].map(aggregated).astype(float), f'daily_{aggregation}_to_week')
        return (pd.Series(np.nan, index=weekly.index, dtype=float), 'missing')

    def primary_score(raw: pd.Series, transform: str) -> pd.Series:
        if transform == 'level_z':
            return rolling_z(raw)
        if transform in {'change_z', 'spread_change_z'}:
            return rolling_z(raw.diff())
        if transform == 'pct_change_z':
            return rolling_z(raw.pct_change())
        if transform == 'momentum4_z':
            return rolling_z(raw.diff(4))
        raise ValueError(f'Unknown causal transform: {transform}')

    def build_causal_features(weekly: pd.DataFrame, daily: pd.DataFrame) -> tuple[pd.DataFrame, list[str], dict[str, str]]:
        features = weekly[['week_id', TARGET_COL, YIELD_COL]].copy()
        feature_cols: list[str] = []
        sources: dict[str, str] = {}
        for spec in CAUSAL_SPECS:
            raw, source = resolve_series(weekly, daily, spec.code, spec.preferred_agg)
            if spec.spread_against:
                other, other_source = resolve_series(weekly, daily, spec.spread_against, spec.preferred_agg)
                raw = raw.sub(other)
                source = f'{source}_spread_against_{other_source}'
            feature_col = spec.feature_column
            transformed = primary_score(raw, spec.transform)
            features[feature_col] = clipped(transformed * spec.yield_sign if spec.role == 'directional' else transformed)
            feature_cols.append(feature_col)
            sources[feature_col] = source
        return (features, feature_cols, sources)

    def attach_calendar(frame: pd.DataFrame, date_map: pd.DataFrame) -> pd.DataFrame:
        required = {'rdate', 'week_id'}
        missing = sorted(required.difference(date_map.columns))
        if missing:
            raise ValueError(f'Date map is missing required columns: {missing}')
        mapped = date_map[['rdate', 'week_id']].copy()
        mapped['rdate'] = pd.to_datetime(mapped['rdate'], errors='coerce')
        mapped['week_id'] = pd.to_numeric(mapped['week_id'], errors='coerce').astype('Int64')
        mapped = mapped.dropna(subset=['rdate', 'week_id'])
        lookup = mapped.groupby('week_id')['rdate'].max()
        out = frame.copy()
        out['week_id'] = pd.to_numeric(out['week_id'], errors='raise').astype(int)
        out = out.sort_values('week_id').reset_index(drop=True)
        out['week_date'] = out['week_id'].map(lookup)
        if out['week_date'].isna().any():
            missing = out.loc[out['week_date'].isna(), 'week_id'].head(10).tolist()
            raise ValueError(f'api_wind_date.csv is missing week mappings: {missing}')
        out['week_date_source'] = 'api_wind_date'
        out['week_date'] = pd.to_datetime(out['week_date'])
        out['month'] = out['week_date'].dt.strftime('%Y-%m')
        return out

    def make_labels(frame: pd.DataFrame) -> pd.DataFrame:
        target = safe_numeric(frame[TARGET_COL])
        future_return = target.shift(-1).div(target).sub(1.0)
        actual = np.select([(future_return > LABEL_THRESHOLD).fillna(False).to_numpy(dtype=bool), (future_return < -LABEL_THRESHOLD).fillna(False).to_numpy(dtype=bool)], [1, -1], default=0).astype(float)
        actual[future_return.isna()] = np.nan
        yield_value = safe_numeric(frame[YIELD_COL])
        future_yield_change = yield_value.shift(-1).sub(yield_value)
        yield_implied = np.select([(future_yield_change < 0).fillna(False).to_numpy(dtype=bool), (future_yield_change > 0).fillna(False).to_numpy(dtype=bool)], [1, -1], default=0).astype(float)
        yield_implied[future_yield_change.isna()] = np.nan
        return pd.DataFrame({'week_id': frame['week_id'], 'week_date': frame['week_date'], 'week_date_source': frame['week_date_source'], 'month': frame['month'], TARGET_COL: target, YIELD_COL: yield_value, 'future_return': future_return, 'actual_label': actual, 'future_yield_change': future_yield_change, 'yield_down_implied_bond_label': yield_implied})

    def build_module_scores(frame: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
        out = frame[['week_id']].copy()
        module_cols: list[str] = []
        for module in MODULE_ORDER:
            columns = [column for column in feature_cols if column.startswith(f'{module}__') and column.endswith('__yield_pressure')]
            score_col = f'{module}_yield_pressure'
            out[score_col] = frame[columns].apply(safe_numeric).mean(axis=1) if columns else np.nan
            out[f'{module}_available_count'] = frame[columns].notna().sum(axis=1) if columns else 0
            module_cols.append(score_col)
        out['yield_pressure_score'] = out[module_cols].mean(axis=1)
        out['bond_price_score'] = -out['yield_pressure_score']
        module_abs = out[module_cols].abs()
        out['top_pressure_module_id'] = module_abs.idxmax(axis=1).str.replace('_yield_pressure', '', regex=False)
        out['top_pressure_module'] = out['top_pressure_module_id'].map(MODULE_CN)
        return out

    def make_model_pipeline(model_type: str, selected_k: int, config: dict[str, Any]) -> Pipeline:
        steps: list[tuple[str, Any]] = [('imputer', SimpleImputer(strategy='median')), ('scaler', StandardScaler())]
        if selected_k > 0:
            steps.append(('selector', SelectKBest(score_func=f_classif, k=selected_k)))
        if model_type == 'logistic':
            steps.append(('model', LogisticRegression(C=float(config['logistic_c']), class_weight='balanced', solver='liblinear', max_iter=1000, random_state=42)))
        else:
            steps.append(('model', RidgeClassifier(class_weight='balanced', alpha=float(config['ridge_alpha']))))
        return Pipeline(steps)

    def fit_ensemble_probabilities(X: pd.DataFrame, y_all: np.ndarray, train_index: np.ndarray, predict_index: np.ndarray, config: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
        x_train = X.iloc[train_index]
        x_predict = X.iloc[predict_index]
        available_columns = x_train.columns[x_train.notna().any(axis=0) & x_train.nunique(dropna=True).gt(1)]
        if len(available_columns) == 0:
            empty = np.full(len(predict_index), np.nan)
            return (empty, empty, empty, 0)
        x_train = x_train[available_columns]
        x_predict = x_predict[available_columns]
        y_train = (y_all[train_index] == 1).astype(int)
        selected_k = min(int(config['k']), len(available_columns))
        logistic_probability = np.full(len(predict_index), np.nan)
        ridge_probability = np.full(len(predict_index), np.nan)
        try:
            logistic = make_model_pipeline('logistic', selected_k, config)
            logistic.fit(x_train, y_train)
            logistic_probability = logistic.predict_proba(x_predict)[:, 1].astype(float)
        except Exception:
            pass
        try:
            ridge = make_model_pipeline('ridge', selected_k, config)
            ridge.fit(x_train, y_train)
            decision = np.asarray(ridge.decision_function(x_predict), dtype=float)
            ridge_probability = 1.0 / (1.0 + np.exp(-np.clip(decision, -35.0, 35.0)))
        except Exception:
            pass
        stacked = np.vstack([logistic_probability, ridge_probability])
        valid_count = np.isfinite(stacked).sum(axis=0)
        total = np.nansum(stacked, axis=0)
        ensemble = np.divide(total, valid_count, out=np.full(len(predict_index), np.nan), where=valid_count > 0)
        return (logistic_probability, ridge_probability, ensemble, selected_k)

    def select_adaptive_configuration(X: pd.DataFrame, y_all: np.ndarray, train_index: np.ndarray) -> dict[str, Any]:
        validation_size = min(ADAPTIVE_VALIDATION_WEEKS, max(26, len(train_index) // 4))
        inner_train = train_index[:-validation_size]
        validation_index = train_index[-validation_size:]
        validation_actual = (y_all[validation_index] == 1).astype(int)
        candidates: list[dict[str, Any]] = []
        if len(inner_train) < 78 or len(np.unique(y_all[inner_train])) < 2:
            return {'config': MODEL_CONFIGS[0], 'threshold': 0.5, 'shrink': 0.75, 'selection_score': np.nan, 'validation_samples': 0}
        for config in MODEL_CONFIGS:
            fit_index = inner_train
            if config['window'] is not None:
                fit_index = fit_index[-int(config['window']):]
            _, _, raw_probability, _ = fit_ensemble_probabilities(X, y_all, fit_index, validation_index, config)
            valid = np.isfinite(raw_probability)
            if valid.sum() < 20 or len(np.unique(validation_actual[valid])) < 2:
                continue
            for shrink in PROBABILITY_SHRINKS:
                calibrated = np.clip(0.5 + float(shrink) * (raw_probability[valid] - 0.5), 0.0, 1.0)
                actual = validation_actual[valid]
                brier = float(brier_score_loss(actual, calibrated))
                for threshold in ADAPTIVE_THRESHOLDS:
                    predicted = (calibrated >= threshold).astype(int)
                    balanced = float(balanced_accuracy_score(actual, predicted))
                    accuracy = float((predicted == actual).mean())
                    score = 0.5 * balanced + 0.25 * accuracy + 0.25 * (1.0 - brier)
                    candidates.append({'config': config, 'threshold': float(threshold), 'shrink': float(shrink), 'selection_score': score, 'validation_samples': int(valid.sum()), 'validation_balanced_accuracy': balanced, 'validation_accuracy': accuracy, 'validation_brier': brier})
        if not candidates:
            return {'config': MODEL_CONFIGS[0], 'threshold': 0.5, 'shrink': 0.75, 'selection_score': np.nan, 'validation_samples': 0}
        return max(candidates, key=lambda item: (item['selection_score'], -abs(item['threshold'] - 0.5), item['shrink']))

    def walk_forward_model(frame: pd.DataFrame, labels: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
        X = frame[feature_cols].apply(safe_numeric)
        y_all = labels['actual_label'].to_numpy()
        rows: list[dict[str, Any]] = []
        selected_cache: dict[str, Any] | None = None
        selected_cache_index = -ADAPTIVE_REFRESH_WEEKS
        for index, row in frame.iterrows():
            week_id = int(row['week_id'])
            if week_id < MODEL_START_WEEK:
                continue
            train_index = np.arange(index)
            train_index = train_index[~pd.isna(y_all[train_index])]
            train_index = train_index[y_all[train_index] != 0]
            if len(train_index) < MIN_TRAIN_WEEKS:
                continue
            if len(np.unique(y_all[train_index])) < 2:
                continue
            if selected_cache is None or index - selected_cache_index >= ADAPTIVE_REFRESH_WEEKS:
                selected_cache = select_adaptive_configuration(X, y_all, train_index)
                selected_cache = {**selected_cache, 'selection_asof_week': week_id}
                selected_cache_index = index
            selected = selected_cache
            config = selected['config']
            fit_index = train_index
            if config['window'] is not None:
                fit_index = fit_index[-int(config['window']):]
            logistic_values, ridge_values, raw_values, selected_k = fit_ensemble_probabilities(X, y_all, fit_index, np.asarray([index]), config)
            raw_probability = float(raw_values[0])
            if pd.isna(raw_probability):
                continue
            probability_up = float(np.clip(0.5 + selected['shrink'] * (raw_probability - 0.5), 0.0, 1.0))
            threshold = float(selected['threshold'])
            rows.append({'week_id': week_id, 'logistic_prob_up': float(logistic_values[0]), 'ridge_prob_up': float(ridge_values[0]), 'raw_prob_up': raw_probability, 'prob_up': probability_up, 'decision_threshold': threshold, 'probability_shrink': float(selected['shrink']), 'pred_label': 1 if probability_up >= threshold else -1, 'model_confidence': abs(probability_up - threshold), 'adaptive_config': str(config['name']), 'adaptive_selection_score': selected['selection_score'], 'adaptive_validation_samples': int(selected['validation_samples']), 'adaptive_validation_balanced_accuracy': selected.get('validation_balanced_accuracy', np.nan), 'adaptive_validation_brier': selected.get('validation_brier', np.nan), 'adaptive_selection_asof_week': int(selected['selection_asof_week']), 'adaptive_selection_age_weeks': int(index - selected_cache_index), 'model_train_samples': int(len(fit_index)), 'model_train_end_week': int(frame.iloc[int(train_index[-1])]['week_id']), 'prediction_asof_week': week_id, 'model_input_feature_count': int(len(feature_cols)), 'model_feature_count': int(selected_k)})
        return pd.DataFrame(rows)

    def build_predictions(frame: pd.DataFrame, labels: pd.DataFrame, model: pd.DataFrame, module_scores: pd.DataFrame) -> pd.DataFrame:
        out = labels.merge(model, on='week_id', how='left')
        out = out.merge(module_scores, on='week_id', how='left')
        out.insert(0, 'strategy_id', STRATEGY_ID)
        out.insert(1, 'strategy_version', VERSION)
        valid = out['pred_label'].isin([-1.0, 1.0]) & out['actual_label'].isin([-1.0, 1.0])
        out['correct'] = pd.NA
        out.loc[valid, 'correct'] = out.loc[valid, 'pred_label'].astype(int).eq(out.loc[valid, 'actual_label'].astype(int))
        out['train_order_valid'] = out['model_train_end_week'].isna() | out['prediction_asof_week'].isna() | out['model_train_end_week'].lt(out['prediction_asof_week'])
        out['data_contract_ready'] = out['week_date_source'].eq('api_wind_date')
        out['prediction_status'] = 'no_model_prediction'
        has_prediction = out['pred_label'].isin([-1.0, 1.0])
        out.loc[has_prediction & out['actual_label'].isna(), 'prediction_status'] = 'forecast_pending_actual'
        out.loc[has_prediction & out['actual_label'].eq(0), 'prediction_status'] = 'neutral_actual_excluded'
        out.loc[valid, 'prediction_status'] = 'scored_direction'
        out.loc[has_prediction & ~out['data_contract_ready'], 'prediction_status'] = 'calendar_mapping_incomplete'
        return out
    _captured = {key: value for key, value in locals().items() if key != '_components'}
    return SimpleNamespace(**_captured)


_COMPONENT_BUILDERS['weekly_1y_causal_production_v1_3_0'] = _build_component_19


def load_components() -> dict[str, SimpleNamespace]:
    """按固定拓扑顺序构建静态组件命名空间。"""
    components: dict[str, SimpleNamespace] = {}
    for module_name in _COMPONENT_ORDER:
        components[module_name] = _COMPONENT_BUILDERS[module_name](components)
    return components


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    predict = subparsers.add_parser(
        "predict",
        help="predict one seven-field Request",
    )
    predict.add_argument("--request", type=Path, required=True)
    predict.add_argument("--data-dir", type=Path, required=True)
    predict.add_argument("--output", type=Path, required=True)

    backtest = subparsers.add_parser(
        "backtest",
        help="predict a batch of one to 100 seven-field Requests",
    )
    backtest.add_argument("--requests", type=Path, required=True)
    backtest.add_argument("--data-dir", type=Path, required=True)
    backtest.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def validate_data_paths(fin_data_dir: Path) -> dict[str, Path]:
    root = fin_data_dir.expanduser().resolve()
    paths = {name: root / name for name in REQUIRED_DATA_FILES}
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Missing required platform data files: " + ", ".join(missing)
        )
    return paths


@contextlib.contextmanager
def restrict_csv_reads(data_paths: dict[str, Path]):
    original = pd.read_csv
    allowed = {path.resolve() for path in data_paths.values()}

    def guarded(source: Any, *args: Any, **kwargs: Any):
        if isinstance(source, (str, os.PathLike)):
            resolved = Path(source).expanduser().resolve()
            if resolved not in allowed:
                raise RuntimeError(
                    "Prediction-only data contract rejected CSV read: "
                    f"{resolved}"
                )
        return original(source, *args, **kwargs)

    pd.read_csv = guarded
    try:
        yield
    finally:
        pd.read_csv = original


def build_context_from_frames(
    modules: dict[str, SimpleNamespace],
    weekly_raw: pd.DataFrame,
    daily_raw: pd.DataFrame,
    date_map: pd.DataFrame,
) -> SimpleNamespace:
    base = modules["weekly_1y_causal_production_v1_6_2"]
    v11 = modules["explore_weekly_1y_causal_v1_11_0"]
    incremental = modules[
        "explore_weekly_1y_causal_v1_12_1_incremental_towers"
    ]
    funding_curve = modules[
        "explore_weekly_1y_causal_v1_12_2_funding_curve"
    ]

    # The frozen component's historical validator accepted an indicator
    # metadata table, but no feature or model ever consumed its rows.  V2 does
    # not provide that non-contract file, so retain only the validator schema.
    metadata = pd.DataFrame(
        columns=["indicators_code", "frequency", "lag_length"]
    )
    base.validate_raw_inputs(
        weekly_raw,
        daily_raw,
        date_map,
        metadata,
    )
    weekly = base.attach_calendar(weekly_raw.copy(), date_map.copy())
    daily, _ = base.prepare_daily(daily_raw.copy(), date_map.copy())
    causal, causal_columns, _ = base.build_causal_features(
        weekly,
        daily,
    )
    causal = weekly[
        ["week_id", "week_date", "week_date_source", "month"]
    ].merge(
        causal,
        on="week_id",
        how="left",
        validate="one_to_one",
    )
    daily_week, daily_columns, _ = v11.build_daily_week_features(
        weekly,
        daily,
    )
    features = causal.merge(
        daily_week,
        on="week_id",
        how="left",
        validate="one_to_one",
    )
    labels = base.make_labels(features)
    weekly_columns = [
        column
        for column in causal_columns
        if column.startswith(
            tuple(
                f"{module}__"
                for module in base.MODEL_INCLUDED_MODULES
            )
        )
    ]
    market_columns = [
        column
        for column in daily_columns
        if column.startswith("dailywk__market_path__")
    ]
    if len(market_columns) != 12:
        raise ValueError(
            f"Expected 12 market-path features, got {len(market_columns)}"
        )

    incremental_frame, groups, _ = (
        incremental.build_incremental_features(weekly, daily)
    )
    curve_frame, curve_columns, _ = (
        funding_curve.build_funding_curve_features(weekly, daily)
    )
    expanded_features = features.merge(
        incremental_frame,
        on="week_id",
        how="left",
        validate="one_to_one",
    )
    full_features = expanded_features.merge(
        curve_frame,
        on="week_id",
        how="left",
        validate="one_to_one",
    )
    return SimpleNamespace(
        weekly_raw=weekly_raw,
        daily_raw=daily_raw,
        date_map=date_map,
        metadata=metadata,
        weekly=weekly,
        daily=daily,
        features=features,
        expanded_features=expanded_features,
        full_features=full_features,
        labels=labels,
        weekly_columns=weekly_columns,
        market_columns=market_columns,
        incremental_groups=groups,
        curve_columns=curve_columns,
    )


def build_context(
    modules: dict[str, SimpleNamespace],
    paths: dict[str, Path],
) -> SimpleNamespace:
    base = modules["weekly_1y_causal_production_v1_6_2"]
    return build_context_from_frames(
        modules,
        base.read_csv(paths["weekly_output.csv"]),
        base.read_csv(paths["daily_output.csv"]),
        base.read_csv(paths["api_wind_date.csv"]),
    )


def build_v13(
    modules: dict[str, SimpleNamespace],
    context: SimpleNamespace,
) -> pd.DataFrame:
    base = modules["weekly_1y_causal_production_v1_6_2"]
    robust = modules[
        "explore_weekly_1y_causal_v1_11_0_market_ensemble"
    ]
    v111 = modules[
        "explore_weekly_1y_causal_v1_11_1_warmup_invariant"
    ]
    incremental = modules[
        "explore_weekly_1y_causal_v1_12_1_incremental_towers"
    ]
    veto = modules[
        "explore_weekly_1y_causal_v1_12_5_v111_confidence_veto"
    ]
    frozen = modules[
        "explore_weekly_1y_causal_v1_13_2_invariant_veto_bag"
    ]

    print(
        "[blackbox-v2] fit V1.13.2 base center",
        file=sys.stderr,
        flush=True,
    )
    base_model = base.walk_forward_model(
        context.features,
        context.labels,
        context.weekly_columns,
    )
    market_model = robust.walk_forward_single_model(
        context.features,
        context.labels,
        context.market_columns,
        {
            "name": "f12__window208",
            "window": 208,
            "k": 12,
            "logistic_c": 0.25,
            "ridge_alpha": 3.0,
        },
    )
    v111_predictions = v111.centered_margin_blend(
        base_model,
        market_model,
        0.925,
        0.5400,
        "v1_11_1_best_center_f12_window208",
    )
    evaluation = context.labels[
        ["week_id", "month", "actual_label"]
    ].merge(
        base_model[["week_id", "prob_up", "pred_label"]].rename(
            columns={
                "prob_up": "base_prob_up",
                "pred_label": "base_pred_label",
            }
        ),
        on="week_id",
        how="inner",
        validate="one_to_one",
    )
    model_cache = evaluation.copy()
    funding_front_columns = [
        *context.incremental_groups["funding_flow"],
        *context.incremental_groups["front_futures"],
    ]
    for window in incremental.MODEL_WINDOWS:
        model_id = f"funding_front__window{window}"
        fitted = robust.walk_forward_single_model(
            context.expanded_features,
            context.labels,
            funding_front_columns,
            {
                "name": model_id,
                "window": window,
                "k": len(funding_front_columns),
                "logistic_c": 0.20,
                "ridge_alpha": 5.0,
            },
        )
        model_cache = model_cache.merge(
            fitted[["week_id", "prob_up"]].rename(
                columns={"prob_up": f"prob_up__{model_id}"}
            ),
            on="week_id",
            how="left",
            validate="one_to_one",
        )

    history_start = frozen.TRUST_HISTORY_START_WEEK_ID
    history_evaluation = evaluation.loc[
        evaluation["week_id"].astype(int).ge(history_start)
    ].copy()
    history_cache = model_cache.loc[
        model_cache["week_id"].astype(int).ge(history_start)
    ].copy()
    history_v111 = v111_predictions.loc[
        v111_predictions["week_id"].astype(int).ge(history_start)
    ].copy()
    funding = veto.funding_event_bag(
        history_cache,
        history_evaluation,
        frozen.FROZEN_TOWER_CENTER,
        frozen.FROZEN_INCREMENTAL_WEIGHT,
    )
    raw_veto = veto.apply_v111_veto(
        funding,
        history_v111,
        frozen.FROZEN_CONFIDENCE_CAP,
    )
    predictions = frozen.bag_online_trust(
        raw_veto,
        history_evaluation,
    )
    scored = history_evaluation.merge(
        predictions[
            [
                "week_id",
                "prob_up",
                "pred_label",
                "funding_veto_applied",
                "trusted_veto_votes",
                "trusted_veto_applied",
            ]
        ],
        on="week_id",
        how="inner",
        validate="one_to_one",
    ).merge(
        history_v111[
            ["week_id", "prob_up", "pred_label"]
        ].rename(
            columns={
                "prob_up": "v111_prob_up",
                "pred_label": "v111_pred_label",
            }
        ),
        on="week_id",
        how="inner",
        validate="one_to_one",
    )
    return scored


def build_v20(
    modules: dict[str, SimpleNamespace],
    context: SimpleNamespace,
    v13_raw: pd.DataFrame,
) -> pd.DataFrame:
    magnitude = modules[
        "explore_weekly_1y_causal_v1_20_0_magnitude_regression"
    ]
    overlay = modules[
        "explore_weekly_1y_causal_v1_14_0_overlay_trust"
    ]
    print(
        "[blackbox-v2] fit V1.20.0 magnitude component",
        file=sys.stderr,
        flush=True,
    )
    feature_groups = {
        "weekly": list(context.weekly_columns),
        "daily": list(context.market_columns),
        "weekly_daily": [
            *context.weekly_columns,
            *context.market_columns,
        ],
    }
    v13 = v13_raw[
        [
            "week_id",
            "base_prob_up",
            "base_pred_label",
            "v111_prob_up",
            "v111_pred_label",
            "prob_up",
            "pred_label",
        ]
    ].copy()
    group_bags: dict[str, pd.DataFrame] = {}
    for group_id, columns in feature_groups.items():
        members = []
        for window in magnitude.WINDOWS:
            for alpha in magnitude.RIDGE_ALPHAS:
                model_id = (
                    f"magnitude_ridge__{group_id}"
                    f"__window{window}__alpha{alpha:.0f}"
                )
                members.append(
                    magnitude.walk_forward_regression(
                        context.features,
                        context.labels,
                        columns,
                        window,
                        alpha,
                        model_id,
                    )
                )
        group_bags[group_id] = overlay.bag_variants(
            members,
            v13,
            f"magnitude_parameter_bag__{group_id}",
        )
    consensus = [
        magnitude.regression_consensus(
            group_bags,
            v13,
            threshold,
            f"magnitude_consensus__confidence{threshold:.2f}",
        )
        for threshold in magnitude.CONFIDENCE_THRESHOLDS
    ]
    predictions = overlay.bag_variants(
        consensus,
        v13,
        "magnitude_invariant_consensus_bag",
    )
    return v13_raw[
        [
            "week_id",
            "month",
            "actual_label",
            "base_prob_up",
            "base_pred_label",
            "prob_up",
            "pred_label",
        ]
    ].rename(
        columns={
            "prob_up": "v13_prob_up",
            "pred_label": "v13_pred_label",
        }
    ).merge(
        predictions[["week_id", "prob_up", "pred_label"]],
        on="week_id",
        how="inner",
        validate="one_to_one",
    )


def fit_tower(
    modules: dict[str, SimpleNamespace],
    context: SimpleNamespace,
    columns: list[str],
    tower_id: str,
) -> pd.DataFrame:
    robust = modules[
        "explore_weekly_1y_causal_v1_11_0_market_ensemble"
    ]
    incremental = modules[
        "explore_weekly_1y_causal_v1_12_1_incremental_towers"
    ]
    merged: pd.DataFrame | None = None
    probability_columns: list[str] = []
    for window in incremental.MODEL_WINDOWS:
        model_id = f"{tower_id}__window{window}"
        fitted = robust.walk_forward_single_model(
            context.full_features,
            context.labels,
            columns,
            {
                "name": model_id,
                "window": window,
                "k": len(columns),
                "logistic_c": 0.20,
                "ridge_alpha": 5.0,
            },
        )
        probability_column = f"prob_up__{model_id}"
        current = fitted[["week_id", "prob_up"]].rename(
            columns={"prob_up": probability_column}
        )
        merged = (
            current
            if merged is None
            else merged.merge(
                current,
                on="week_id",
                how="inner",
                validate="one_to_one",
            )
        )
        probability_columns.append(probability_column)
    if merged is None:
        raise RuntimeError(f"No tower members for {tower_id}")
    merged[f"{tower_id}_prob_up"] = merged[
        probability_columns
    ].median(axis=1)
    return merged[["week_id", f"{tower_id}_prob_up"]]


def build_v25(
    modules: dict[str, SimpleNamespace],
    context: SimpleNamespace,
    v13_raw: pd.DataFrame,
) -> pd.DataFrame:
    robust = modules[
        "explore_weekly_1y_causal_v1_11_0_market_ensemble"
    ]
    chooser = modules[
        "explore_weekly_1y_causal_v1_14_6_event_chooser"
    ]
    router = modules[
        "explore_weekly_1y_causal_v1_25_0_target_state_event_router"
    ]
    frozen = modules[
        "explore_weekly_1y_causal_v1_25_1_invariant_daily_event_router"
    ]
    overlay = modules[
        "explore_weekly_1y_causal_v1_14_0_overlay_trust"
    ]
    print(
        "[blackbox-v2] fit V1.25.1 daily router",
        file=sys.stderr,
        flush=True,
    )
    market_model = robust.walk_forward_single_model(
        context.full_features,
        context.labels,
        context.market_columns,
        {
            "name": "f12__window208",
            "window": 208,
            "k": 12,
            "logistic_c": 0.25,
            "ridge_alpha": 3.0,
        },
    )[["week_id", "prob_up"]].rename(
        columns={"prob_up": "v111_market_prob_up"}
    )
    funding_front_columns = [
        *context.incremental_groups["funding_flow"],
        *context.incremental_groups["front_futures"],
    ]
    funding_front = fit_tower(
        modules,
        context,
        funding_front_columns,
        "funding_front",
    )
    duration = fit_tower(
        modules,
        context,
        context.incremental_groups["duration_positioning"],
        "duration",
    )
    curve = fit_tower(
        modules,
        context,
        context.curve_columns,
        "funding_curve",
    )
    panel = v13_raw[
        [
            "week_id",
            "month",
            "actual_label",
            "base_prob_up",
            "base_pred_label",
            "v111_prob_up",
            "v111_pred_label",
            "prob_up",
            "pred_label",
        ]
    ].rename(
        columns={
            "prob_up": "v13_prob_up",
            "pred_label": "v13_pred_label",
        }
    ).merge(
        market_model,
        on="week_id",
        how="inner",
        validate="one_to_one",
    ).merge(
        funding_front,
        on="week_id",
        how="inner",
        validate="one_to_one",
    ).merge(
        duration,
        on="week_id",
        how="inner",
        validate="one_to_one",
    ).merge(
        curve,
        on="week_id",
        how="inner",
        validate="one_to_one",
    ).merge(
        context.full_features[
            ["week_id", *context.market_columns]
        ],
        on="week_id",
        how="left",
        validate="one_to_one",
    )
    panel["base_abs_margin"] = (
        panel["base_prob_up"] - 0.475
    ).abs()
    panel["v13_abs_margin"] = (
        panel["v13_prob_up"] - 0.475
    ).abs()
    panel["v13_direction"] = panel["v13_pred_label"].astype(float)
    panel["market_margin"] = panel["v111_market_prob_up"] - 0.540
    panel["funding_front_margin"] = (
        panel["funding_front_prob_up"] - 0.535
    )
    panel["funding_curve_margin"] = (
        panel["funding_curve_prob_up"] - 0.500
    )
    panel["duration_margin"] = panel["duration_prob_up"] - 0.525
    panel["disagreement"] = (
        panel["v13_pred_label"].astype(int)
        != panel["base_pred_label"].astype(int)
    )
    panel["v13_correct"] = np.where(
        panel["actual_label"].isin((-1.0, 1.0)),
        (
            panel["v13_pred_label"].astype(float)
            == panel["actual_label"].astype(float)
        ).astype(float),
        np.nan,
    )
    feature_columns = (*router.MODEL_COLUMNS, *context.market_columns)
    v13 = panel[
        [
            "week_id",
            "base_prob_up",
            "base_pred_label",
            "v111_prob_up",
            "v111_pred_label",
            "v13_prob_up",
            "v13_pred_label",
        ]
    ].rename(
        columns={
            "v13_prob_up": "prob_up",
            "v13_pred_label": "pred_label",
        }
    )
    members = []
    for event_window in frozen.EVENT_WINDOWS:
        for logistic_c in frozen.LOGISTIC_CS:
            members.append(
                chooser.walk_forward_choose(
                    panel,
                    feature_columns,
                    event_window,
                    logistic_c,
                    (
                        f"daily_event_router__events{event_window}"
                        f"__c{logistic_c:.2f}"
                    ),
                )
            )
    predictions = overlay.bag_variants(
        members,
        v13,
        "invariant_daily_event_router_bag",
    )
    return panel[
        [
            "week_id",
            "month",
            "actual_label",
            "base_prob_up",
            "base_pred_label",
            "v13_prob_up",
            "v13_pred_label",
        ]
    ].merge(
        predictions[["week_id", "prob_up", "pred_label"]],
        on="week_id",
        how="inner",
        validate="one_to_one",
    )


def build_v13_expert(
    modules: dict[str, SimpleNamespace],
    context: SimpleNamespace,
) -> pd.DataFrame:
    expert = modules["weekly_1y_causal_production_v1_3_0"]
    print(
        "[blackbox-v2] fit V1.3/V1.4 twin expert",
        file=sys.stderr,
        flush=True,
    )
    expert.validate_raw_inputs(
        context.weekly_raw.copy(),
        context.daily_raw.copy(),
        context.date_map.copy(),
        context.metadata.copy(),
    )
    weekly = expert.attach_calendar(
        context.weekly_raw.copy(),
        context.date_map.copy(),
    )
    daily, _ = expert.prepare_daily(
        context.daily_raw.copy(),
        context.date_map.copy(),
    )
    causal, feature_columns, _ = expert.build_causal_features(
        weekly,
        daily,
    )
    causal = weekly[
        ["week_id", "week_date", "week_date_source", "month"]
    ].merge(
        causal,
        on="week_id",
        how="left",
        validate="one_to_one",
    )
    labels = expert.make_labels(causal)
    module_scores = expert.build_module_scores(
        causal,
        feature_columns,
    )
    model_columns = [
        column
        for column in feature_columns
        if column.startswith(
            tuple(
                f"{module}__"
                for module in expert.MODEL_INCLUDED_MODULES
            )
        )
    ]
    model = expert.walk_forward_model(
        causal,
        labels,
        model_columns,
    )
    return expert.build_predictions(
        causal,
        labels,
        model,
        module_scores,
    )


def build_v27(
    modules: dict[str, SimpleNamespace],
    v13_raw: pd.DataFrame,
    magnitude: pd.DataFrame,
    router: pd.DataFrame,
    expert_raw: pd.DataFrame,
) -> pd.DataFrame:
    gate = modules[
        "explore_weekly_1y_causal_v1_26_0_cross_expert_veto"
    ]
    sparse = modules[
        "explore_weekly_1y_causal_v1_27_0_orthogonal_sparse_union"
    ]
    overlay = modules[
        "explore_weekly_1y_causal_v1_14_0_overlay_trust"
    ]
    print(
        "[blackbox-v2] combine V1.27.2 center",
        file=sys.stderr,
        flush=True,
    )
    v13 = v13_raw[
        [
            "week_id",
            "base_prob_up",
            "base_pred_label",
            "v111_prob_up",
            "v111_pred_label",
            "prob_up",
            "pred_label",
        ]
    ].copy()
    router_frame = router[
        [
            "week_id",
            "base_prob_up",
            "base_pred_label",
            "v13_prob_up",
            "v13_pred_label",
            "prob_up",
            "pred_label",
        ]
    ].merge(
        v13[["week_id", "v111_prob_up", "v111_pred_label"]],
        on="week_id",
        how="left",
        validate="one_to_one",
    )
    expert = expert_raw[
        ["week_id", "prob_up", "pred_label"]
    ].dropna(subset=["prob_up", "pred_label"])
    members = []
    for expert_id in ("v1_3", "v1_4"):
        gated = gate.gate_router_with_expert(
            router_frame,
            expert,
            expert_id,
        )
        members.append(
            sparse.combine_sparse_overlays(
                v13,
                magnitude[["week_id", "prob_up", "pred_label"]],
                gated,
                f"twin_expert_union__{expert_id}",
            )
        )
    if not members[0]["pred_label"].astype(int).equals(
        members[1]["pred_label"].astype(int)
    ):
        raise RuntimeError("V1.27.2 twin experts diverged")
    return overlay.bag_variants(
        members,
        v13,
        "twin_expert_confirmed_sparse_union",
    )


def make_v128_model():
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    return Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    C=0.10,
                    class_weight="balanced",
                    solver="liblinear",
                    max_iter=1000,
                    random_state=42,
                ),
            ),
        ]
    )


def apply_v128(
    panel: pd.DataFrame,
    center: pd.DataFrame,
) -> pd.DataFrame:
    feature_columns = (
        "dailywk__market_path__TB1YWI0W__mean_z52",
        "target_change_lag1",
    )
    frame = panel[
        ["week_id", "actual_label", *feature_columns]
    ].merge(
        center[
            [
                "week_id",
                "base_pred_label",
                "prob_up",
                "pred_label",
            ]
        ].rename(
            columns={
                "prob_up": "center_prob_up",
                "pred_label": "center_pred_label",
            }
        ),
        on="week_id",
        how="inner",
        validate="one_to_one",
    ).sort_values("week_id").reset_index(drop=True)
    prior_events: list[int] = []
    prior_scores: list[float] = []
    rows: list[dict[str, Any]] = []
    for index, row in frame.iterrows():
        center_decline = int(row["center_pred_label"]) == -1
        base_decline = int(row["base_pred_label"]) == -1
        train_indices = [
            event_index
            for event_index in prior_events[-64:]
            if frame.loc[event_index, "actual_label"] in (-1.0, 1.0)
        ]
        probability = np.nan
        percentile = np.nan
        available = (
            center_decline
            and len(train_indices) >= 40
            and frame.loc[train_indices, "actual_label"].nunique() >= 2
        )
        if available:
            model = make_v128_model()
            model.fit(
                frame.loc[train_indices, list(feature_columns)],
                frame.loc[train_indices, "actual_label"]
                .eq(1.0)
                .astype(int),
            )
            probability = float(
                model.predict_proba(
                    frame.loc[[index], list(feature_columns)]
                )[0, 1]
            )
            history = prior_scores[-64:]
            if len(history) >= 20:
                values = np.asarray(history, dtype=float)
                percentile = float(
                    (np.count_nonzero(values <= probability) + 1)
                    / (len(values) + 1)
                )
        proposal = bool(
            available and base_decline and percentile >= 0.90
        )
        rows.append(
            {
                "week_id": int(row["week_id"]),
                "prob_up": (
                    max(float(row["center_prob_up"]), probability)
                    if proposal
                    else float(row["center_prob_up"])
                ),
                "pred_label": (
                    1 if proposal else int(row["center_pred_label"])
                ),
            }
        )
        if available:
            prior_scores.append(probability)
        if center_decline and row["actual_label"] in (-1.0, 1.0):
            prior_events.append(index)
    return pd.DataFrame(rows)


def build_final_predictions(
    context: SimpleNamespace,
    v13: pd.DataFrame,
    v27: pd.DataFrame,
) -> pd.DataFrame:
    labels = context.labels.copy()
    target = pd.to_numeric(labels["TB1YWI3C"], errors="coerce")
    target_path = labels[["week_id"]].copy()
    target_path["target_change_lag1"] = target.diff().shift(1)
    panel = labels[
        ["week_id", "month", "actual_label"]
    ].merge(
        context.features[
            ["week_id", *context.market_columns]
        ],
        on="week_id",
        how="left",
        validate="one_to_one",
    ).merge(
        target_path,
        on="week_id",
        how="left",
        validate="one_to_one",
    )
    center = v13[
        [
            "week_id",
            "base_prob_up",
            "base_pred_label",
            "v111_prob_up",
            "v111_pred_label",
        ]
    ].merge(
        v27[["week_id", "prob_up", "pred_label"]],
        on="week_id",
        how="inner",
        validate="one_to_one",
    )
    prediction = apply_v128(panel, center)
    scored = panel[
        ["week_id", "month", "actual_label"]
    ].merge(
        center[["week_id", "prob_up", "pred_label"]].rename(
            columns={
                "prob_up": "center_prob_up",
                "pred_label": "center_pred_label",
            }
        ),
        on="week_id",
        how="inner",
        validate="one_to_one",
    ).merge(
        prediction,
        on="week_id",
        how="inner",
        validate="one_to_one",
    )
    scored.insert(0, "strategy_id", STRATEGY_ID)
    valid = scored["actual_label"].isin((-1.0, 1.0))
    scored["correct"] = pd.NA
    scored.loc[valid, "correct"] = (
        scored.loc[valid, "pred_label"].astype(int)
        == scored.loc[valid, "actual_label"].astype(int)
    )
    scored["changed_from_v1_27_2"] = (
        scored["pred_label"].astype(int)
        != scored["center_pred_label"].astype(int)
    )
    return scored


def rebuild_v128_center_from_frames(
    weekly: pd.DataFrame,
    daily: pd.DataFrame,
    date_map: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    modules = load_components()
    context = build_context_from_frames(
        modules,
        weekly.copy(),
        daily.copy(),
        date_map.copy(),
    )
    v13 = build_v13(modules, context)
    v20 = build_v20(modules, context, v13)
    v25 = build_v25(modules, context, v13)
    expert = build_v13_expert(modules, context)
    v27 = build_v27(modules, v13, v20, v25, expert)
    center = build_final_predictions(context, v13, v27)
    return center, weekly.reset_index(drop=True).copy()


def rebuild_v128_center(
    paths: dict[str, Path],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    with restrict_csv_reads(paths):
        weekly = pd.read_csv(
            paths["weekly_output.csv"], encoding="utf-8-sig"
        )
        daily = pd.read_csv(
            paths["daily_output.csv"], encoding="utf-8-sig"
        )
        date_map = pd.read_csv(
            paths["api_wind_date.csv"], encoding="utf-8-sig"
        )
    return rebuild_v128_center_from_frames(weekly, daily, date_map)


def build_curve_state(weekly: pd.DataFrame) -> pd.DataFrame:
    required = {"week_id", "ZY000073", "ZY000075"}
    missing = sorted(required.difference(weekly.columns))
    if missing:
        raise ValueError(
            "weekly_output.csv is missing curve-trust columns: "
            + ", ".join(missing)
        )
    one_year = pd.to_numeric(weekly["ZY000073"], errors="coerce")
    three_year = pd.to_numeric(weekly["ZY000075"], errors="coerce")
    spread = three_year - one_year
    prior = spread.shift(1).rolling(
        SPREAD_Z_WINDOW,
        min_periods=SPREAD_Z_MIN_PERIODS,
    )
    spread_z = spread.sub(prior.mean()).div(
        prior.std(ddof=0).replace(0, np.nan)
    ).clip(-3.0, 3.0)
    return pd.DataFrame(
        {
            "week_id": pd.to_numeric(weekly["week_id"], errors="raise").astype(int),
            "curve_3y_minus_1y": spread,
            "curve_3y_minus_1y_z52": spread_z,
        }
    )


def apply_gray_overlay(base: pd.DataFrame) -> pd.DataFrame:
    out = base.sort_values("week_id").reset_index(drop=True).copy()
    out["raw_curve_event"] = (
        out["production_pred_label"].eq(1)
        & out["curve_3y_minus_1y_z52"].le(SPREAD_Z_THRESHOLD)
    ).fillna(False)
    out["adjacent_event_block"] = out["raw_curve_event"].shift(
        1,
        fill_value=False,
    )
    out["trust_prior_event_count"] = 0
    out["trust_prior_event_wins"] = 0
    out["trust_last_two_events_won"] = False
    out["gray_overlay_candidate"] = False
    out["counterfactual_event_outcome"] = pd.NA

    realized_event_outcomes: list[int] = []
    for index, row in out.iterrows():
        raw_event = bool(row["raw_curve_event"])
        last_two_won = bool(
            len(realized_event_outcomes) >= TRUST_EVENT_WINS
            and all(
                outcome == 1
                for outcome in realized_event_outcomes[-TRUST_EVENT_WINS:]
            )
        )
        out.at[index, "trust_prior_event_count"] = len(
            realized_event_outcomes
        )
        out.at[index, "trust_prior_event_wins"] = sum(
            outcome == 1 for outcome in realized_event_outcomes
        )
        out.at[index, "trust_last_two_events_won"] = last_two_won
        out.at[index, "gray_overlay_candidate"] = bool(
            raw_event
            and last_two_won
            and not bool(row["adjacent_event_block"])
        )

        if raw_event and row["actual_label"] in (-1.0, 1.0):
            counterfactual_direction = -int(row["production_pred_label"])
            outcome = int(
                counterfactual_direction == int(row["actual_label"])
            )
            realized_event_outcomes.append(outcome)
            out.at[index, "counterfactual_event_outcome"] = outcome

    out["shadow_pred_label"] = np.where(
        out["gray_overlay_candidate"],
        -out["production_pred_label"].astype(int),
        out["production_pred_label"].astype(int),
    ).astype(int)
    out["production_override_enabled"] = False
    out["official_pred_label"] = out["production_pred_label"].astype(int)
    valid = out["actual_label"].isin((-1.0, 1.0))
    out["shadow_correct"] = pd.NA
    out["production_correct"] = pd.NA
    out.loc[valid, "shadow_correct"] = (
        out.loc[valid, "shadow_pred_label"].astype(int)
        == out.loc[valid, "actual_label"].astype(int)
    )
    out.loc[valid, "production_correct"] = (
        out.loc[valid, "production_pred_label"].astype(int)
        == out.loc[valid, "actual_label"].astype(int)
    )
    return out


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"request.json contains duplicate field: {key}")
        result[key] = value
    return result


def _validate_request(raw: Any, source: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"{source} must contain one JSON object")
    actual = set(raw)
    expected = set(REQUEST_FIELDS)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise ValueError(
            f"{source} fields must be exactly {list(REQUEST_FIELDS)}; "
            f"missing={missing}, extra={extra}"
        )
    if any(not isinstance(raw[field], str) for field in REQUEST_FIELDS):
        raise ValueError(f"{source} Request values must all be strings")
    request = {field: raw[field] for field in REQUEST_FIELDS}
    if not request["request_id"].strip():
        raise ValueError(f"{source} request_id must be non-empty")

    for field in (
        "predict_date",
        "feature_date",
        "target_date",
        "daily_cutoff_key",
    ):
        value = request[field]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise ValueError(f"{source} {field} must be YYYY-MM-DD")
        try:
            parsed = pd.to_datetime(value, format="%Y-%m-%d", errors="raise")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{source} {field} is not a valid date") from exc
        if parsed.strftime("%Y-%m-%d") != value:
            raise ValueError(f"{source} {field} must be canonical YYYY-MM-DD")

    for field in ("weekly_cutoff_key", "monthly_cutoff_key"):
        if re.fullmatch(r"\d{6}", request[field]) is None:
            raise ValueError(f"{source} {field} must be a six-digit string")

    feature_date = request["feature_date"]
    predict_date = request["predict_date"]
    target_date = request["target_date"]
    if not (
        feature_date <= predict_date <= target_date
        and feature_date < target_date
    ):
        raise ValueError(
            f"{source} dates must satisfy feature_date <= predict_date "
            "<= target_date and feature_date < target_date"
        )
    return request


def _load_predict_request(path: Path) -> dict[str, str]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(
                handle,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read valid UTF-8 JSON Request: {path}") from exc
    return _validate_request(raw, "request.json")


def _read_csv_header(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle), [])
    except OSError as exc:
        raise ValueError(f"cannot read CSV: {path}") from exc
    if not header:
        raise ValueError(f"CSV header is empty: {path}")
    if len(header) != len(set(header)):
        raise ValueError(f"CSV contains duplicate columns: {path}")
    return header


def _load_backtest_requests(path: Path) -> list[dict[str, str]]:
    header = _read_csv_header(path)
    if set(header) != set(REQUEST_FIELDS) or len(header) != len(REQUEST_FIELDS):
        missing = sorted(set(REQUEST_FIELDS) - set(header))
        extra = sorted(set(header) - set(REQUEST_FIELDS))
        raise ValueError(
            "requests.csv fields must be exactly "
            f"{list(REQUEST_FIELDS)}; missing={missing}, extra={extra}"
        )
    try:
        frame = pd.read_csv(
            path,
            dtype="string",
            keep_default_na=False,
            encoding="utf-8-sig",
        )
    except Exception as exc:
        raise ValueError(f"cannot parse requests.csv: {path}") from exc
    if not 1 <= len(frame) <= 100:
        raise ValueError("requests.csv must contain between 1 and 100 rows")
    requests = [
        _validate_request(
            {field: row[field] for field in REQUEST_FIELDS},
            f"requests.csv row {index + 2}",
        )
        for index, row in frame.iterrows()
    ]
    request_ids = [request["request_id"] for request in requests]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("requests.csv request_id values must be unique")
    return requests


def _load_string_csv(path: Path) -> pd.DataFrame:
    _read_csv_header(path)
    try:
        return pd.read_csv(
            path,
            dtype="string",
            keep_default_na=False,
            encoding="utf-8-sig",
        )
    except Exception as exc:
        raise ValueError(f"cannot parse DataBridge CSV: {path}") from exc


def _validate_required_columns(
    frame: pd.DataFrame,
    filename: str,
    required: tuple[str, ...],
) -> None:
    actual = list(frame.columns)
    if not actual or actual[0] != required[0]:
        raise ValueError(f"{filename} must start with {required[0]}")
    missing = sorted(set(required) - set(actual))
    if missing:
        raise ValueError(
            f"{filename} is missing consumed columns: {missing}"
        )
    if frame.empty:
        raise ValueError(f"{filename} must not be empty")


def _validate_numeric_columns(
    frame: pd.DataFrame,
    filename: str,
    columns: tuple[str, ...],
) -> None:
    for column in columns:
        text_values = frame[column].astype("string")
        nonempty = text_values.str.strip().ne("")
        numeric = pd.to_numeric(
            text_values.where(nonempty),
            errors="coerce",
        )
        finite = np.isfinite(
            numeric.to_numpy(dtype=float, na_value=np.nan)
        )
        invalid = nonempty.to_numpy(dtype=bool) & ~finite
        if invalid.any():
            first = int(np.flatnonzero(invalid)[0]) + 2
            raise ValueError(
                f"{filename} row {first} column {column} must be a "
                "finite number or empty"
            )


def _canonical_dates(
    values: pd.Series,
    filename: str,
    column: str,
) -> pd.Series:
    text_values = values.astype("string")
    if not text_values.str.fullmatch(r"\d{4}-\d{2}-\d{2}").all():
        raise ValueError(f"{filename} {column} must contain YYYY-MM-DD")
    try:
        parsed = pd.to_datetime(
            text_values,
            format="%Y-%m-%d",
            errors="raise",
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{filename} {column} contains an invalid date") from exc
    canonical = pd.Series(parsed).dt.strftime("%Y-%m-%d")
    if not np.array_equal(
        canonical.to_numpy(dtype=str),
        text_values.to_numpy(dtype=str),
    ):
        raise ValueError(f"{filename} {column} dates must be canonical")
    return pd.Series(parsed, index=values.index)


def _load_contract_data(data_dir: Path) -> dict[str, Any]:
    paths = validate_data_paths(data_dir)
    weekly = _load_string_csv(paths["weekly_output.csv"])
    daily = _load_string_csv(paths["daily_output.csv"])
    calendar = _load_string_csv(paths["api_wind_date.csv"])

    _validate_required_columns(
        weekly,
        "weekly_output.csv",
        REQUIRED_WEEKLY_COLUMNS,
    )
    _validate_required_columns(
        daily,
        "daily_output.csv",
        REQUIRED_DAILY_COLUMNS,
    )
    if list(calendar.columns) != ["rdate", "week_id"]:
        raise ValueError(
            "api_wind_date.csv columns must be exactly rdate,week_id"
        )
    if calendar.empty:
        raise ValueError("api_wind_date.csv must not be empty")

    weekly_keys = weekly["week_id"].astype("string")
    if not weekly_keys.str.fullmatch(r"\d{6}").all():
        raise ValueError("weekly_output.csv week_id must be six digits")
    if weekly_keys.duplicated().any() or not weekly_keys.is_monotonic_increasing:
        raise ValueError(
            "weekly_output.csv week_id must be unique and ascending"
        )
    daily_dates = _canonical_dates(
        daily["date"], "daily_output.csv", "date"
    )
    if daily["date"].duplicated().any() or not daily_dates.is_monotonic_increasing:
        raise ValueError("daily_output.csv date must be unique and ascending")

    calendar_dates = _canonical_dates(
        calendar["rdate"], "api_wind_date.csv", "rdate"
    )
    calendar_weeks = calendar["week_id"].astype("string")
    if not calendar_weeks.str.fullmatch(r"\d{6}").all():
        raise ValueError("api_wind_date.csv week_id must be six digits")
    if (
        calendar["rdate"].duplicated().any()
        or not calendar_dates.is_monotonic_increasing
    ):
        raise ValueError(
            "api_wind_date.csv rdate must be unique and ascending"
        )

    calendar_by_date = dict(
        zip(calendar["rdate"].tolist(), calendar_weeks.tolist())
    )

    _validate_numeric_columns(
        weekly,
        "weekly_output.csv",
        REQUIRED_WEEKLY_COLUMNS[1:],
    )
    _validate_numeric_columns(
        daily,
        "daily_output.csv",
        REQUIRED_DAILY_COLUMNS[1:],
    )
    return {
        "paths": paths,
        "weekly": weekly,
        "daily": daily,
        "calendar": calendar,
        "weekly_positions": {
            key: index for index, key in enumerate(weekly_keys.tolist())
        },
        "daily_positions": {
            key: index for index, key in enumerate(daily["date"].tolist())
        },
        "calendar_by_date": calendar_by_date,
    }


def _request_positions(
    data: dict[str, Any],
    request: dict[str, str],
) -> tuple[int, int]:
    weekly_key = request["weekly_cutoff_key"]
    daily_key = request["daily_cutoff_key"]
    if weekly_key not in data["weekly_positions"]:
        raise ValueError(
            f"weekly cutoff key not found: {weekly_key}"
        )
    if daily_key not in data["daily_positions"]:
        raise ValueError(f"daily cutoff key not found: {daily_key}")
    mapped_week = data["calendar_by_date"].get(daily_key)
    if mapped_week != weekly_key:
        raise ValueError(
            f"calendar maps {daily_key} to {mapped_week!r}, not "
            f"Request weekly_cutoff_key {weekly_key!r}"
        )
    return (
        int(data["weekly_positions"][weekly_key]),
        int(data["daily_positions"][daily_key]),
    )


def _build_internal_predictions(
    weekly: pd.DataFrame,
    daily: pd.DataFrame,
    calendar: pd.DataFrame,
) -> pd.DataFrame:
    calendar_weeks = set(calendar["week_id"].astype("string"))
    missing = sorted(set(weekly["week_id"].astype("string")) - calendar_weeks)
    if missing:
        raise ValueError(
            "truncated calendar has no mapping for weekly observations: "
            f"{missing[:10]}"
        )
    calendar_dates = set(calendar['rdate'].astype('string'))
    missing_daily = sorted(set(daily['date'].astype('string')) - calendar_dates)
    if missing_daily:
        raise ValueError(f'truncated calendar has no mapping for daily observations: {missing_daily[:10]}')
    center, weekly_for_curve = rebuild_v128_center_from_frames(
        weekly,
        daily,
        calendar,
    )
    curve = build_curve_state(weekly_for_curve)
    base = center[
        ["week_id", "month", "actual_label", "prob_up", "pred_label"]
    ].rename(
        columns={
            "prob_up": "production_prob_up",
            "pred_label": "production_pred_label",
        }
    ).merge(curve, on="week_id", how="left", validate="one_to_one")
    base["year"] = base["month"].astype(str).str[:4]
    predictions = apply_gray_overlay(base)
    if predictions.empty:
        raise ValueError("insufficient truncated history to generate a prediction")
    return predictions


def _internal_week_key(value: Any) -> str:
    text = str(value)
    if text.endswith(".0"):
        text = text[:-2]
    if re.fullmatch(r"\d{1,6}", text) is None:
        raise ValueError(f"model produced invalid week_id: {value!r}")
    return text.zfill(6)


def _extract_internal_prediction(
    predictions: pd.DataFrame,
    weekly_cutoff_key: str,
) -> dict[str, Any]:
    keys = predictions["week_id"].map(_internal_week_key)
    matches = predictions.loc[keys.eq(weekly_cutoff_key)]
    if len(matches) != 1:
        raise ValueError(
            "model did not produce exactly one prediction for weekly cutoff "
            f"{weekly_cutoff_key}"
        )
    row = matches.iloc[0]
    direction = int(row["shadow_pred_label"])
    production_direction = int(row["production_pred_label"])
    probability = float(row["production_prob_up"])
    if direction not in (-1, 1) or production_direction not in (-1, 1):
        raise ValueError("model produced an invalid direction")
    if not np.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ValueError("model produced an invalid probability")
    return {
        "predicted_direction": direction,
        "model_probability": probability,
        "production_direction": production_direction,
        "gray_overlay_candidate": bool(row["gray_overlay_candidate"]),
    }


def _compute_independent(
    data: dict[str, Any],
    request: dict[str, str],
) -> dict[str, Any]:
    weekly_position, daily_position = _request_positions(data, request)
    weekly = data["weekly"].iloc[: weekly_position + 1].copy()
    daily = data["daily"].iloc[: daily_position + 1].copy()
    calendar = data["calendar"].loc[
        data["calendar"]["rdate"].le(request["daily_cutoff_key"])
    ].copy()
    predictions = _build_internal_predictions(weekly, daily, calendar)
    return _extract_internal_prediction(
        predictions,
        request["weekly_cutoff_key"],
    )


def _independent_batch(
    data: dict[str, Any],
    requests: list[dict[str, str]],
) -> list[dict[str, Any]]:
    cache: dict[tuple[str, str], dict[str, Any]] = {}
    output: list[dict[str, Any]] = []
    for request in requests:
        key = (
            request["daily_cutoff_key"],
            request["weekly_cutoff_key"],
        )
        if key not in cache:
            cache[key] = _compute_independent(data, request)
        output.append(cache[key].copy())
    return output


def _same_internal_prediction(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    return bool(
        left["predicted_direction"] == right["predicted_direction"]
        and left["production_direction"] == right["production_direction"]
        and left["gray_overlay_candidate"]
        == right["gray_overlay_candidate"]
        and left["model_probability"] == right["model_probability"]
    )


def _can_optimize_batch(
    data: dict[str, Any],
    requests: list[dict[str, str]],
    positions: list[tuple[int, int]],
    maximum_index: int,
) -> bool:
    maximum_weekly, maximum_daily = positions[maximum_index]
    if maximum_weekly != max(position[0] for position in positions):
        return False
    for request, (_, daily_position) in zip(requests, positions):
        later_dates = data["daily"].iloc[
            daily_position + 1 : maximum_daily + 1
        ]["date"]
        if any(
            data["calendar_by_date"][date]
            == request["weekly_cutoff_key"]
            for date in later_dates
        ):
            return False
    return True


def _backtest_predictions(
    data: dict[str, Any],
    requests: list[dict[str, str]],
) -> list[dict[str, Any]]:
    if len(requests) <= 3:
        return _independent_batch(data, requests)

    positions = [_request_positions(data, request) for request in requests]
    maximum_index = max(
        range(len(requests)),
        key=lambda index: positions[index][1],
    )
    if not _can_optimize_batch(
        data,
        requests,
        positions,
        maximum_index,
    ):
        print(
            "[blackbox-v2] batch optimization not applicable; "
            "using independent truncation",
            file=sys.stderr,
        )
        return _independent_batch(data, requests)

    maximum_request = requests[maximum_index]
    maximum_weekly, maximum_daily = positions[maximum_index]
    weekly = data["weekly"].iloc[: maximum_weekly + 1].copy()
    daily = data["daily"].iloc[: maximum_daily + 1].copy()
    calendar = data["calendar"].loc[
        data["calendar"]["rdate"].le(
            maximum_request["daily_cutoff_key"]
        )
    ].copy()
    optimized_frame = _build_internal_predictions(weekly, daily, calendar)
    optimized = [
        _extract_internal_prediction(
            optimized_frame,
            request["weekly_cutoff_key"],
        )
        for request in requests
    ]

    sample_indices = sorted({0, len(requests) // 2, len(requests) - 1})
    for index in sample_indices:
        independent = _compute_independent(data, requests[index])
        if not _same_internal_prediction(optimized[index], independent):
            print(
                "[blackbox-v2] batch causal self-check mismatch; "
                "falling back to independent truncation",
                file=sys.stderr,
            )
            return _independent_batch(data, requests)
    print(
        "[blackbox-v2] batch causal self-check passed for first/middle/last",
        file=sys.stderr,
    )
    return optimized


def _result_rows(
    requests: list[dict[str, str]],
    predictions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(requests) != len(predictions):
        raise RuntimeError("internal result cardinality mismatch")
    return [
        {
            "request_id": request["request_id"],
            "predict_date": request["predict_date"],
            "feature_date": request["feature_date"],
            "target_date": request["target_date"],
            "predicted_direction": int(prediction["predicted_direction"]),
        }
        for request, prediction in zip(requests, predictions)
    ]


def _prepare_output_path(output: Path, data_dir: Path) -> Path:
    resolved = output.expanduser().resolve()
    data_root = data_dir.expanduser().resolve()
    if resolved == data_root or data_root in resolved.parents:
        raise ValueError("--output must not be inside the read-only --data-dir")
    if resolved.exists():
        raise FileExistsError(f"--output already exists: {resolved}")
    if not resolved.parent.is_dir():
        raise FileNotFoundError(
            f"--output parent directory does not exist: {resolved.parent}"
        )
    return resolved


def _atomic_write(
    output: Path,
    writer: Any,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        writer(temporary)
        if temporary.stat().st_size > 50 * 1024 * 1024:
            raise ValueError("Output exceeds the 50 MiB Contract limit")
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_prediction_json(
    output: Path,
    result: dict[str, Any],
) -> None:
    def writer(path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    _atomic_write(output, writer)


def _write_backtest_csv(
    output: Path,
    results: list[dict[str, Any]],
) -> None:
    def writer(path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            csv_writer = csv.DictWriter(
                handle,
                fieldnames=list(RESULT_FIELDS),
                lineterminator="\n",
            )
            csv_writer.writeheader()
            csv_writer.writerows(results)
            handle.flush()
            os.fsync(handle.fileno())

    _atomic_write(output, writer)


def run_contract(args: argparse.Namespace) -> None:
    if Path(__file__).stem != SCHEME_ID:
        raise ValueError(
            f"delivery filename must be {SCHEME_ID}.py"
        )
    output = _prepare_output_path(args.output, args.data_dir)
    if args.command == "predict":
        request = _load_predict_request(args.request)
        data = _load_contract_data(args.data_dir)
        prediction = _compute_independent(data, request)
        result = _result_rows([request], [prediction])[0]
        _write_prediction_json(output, result)
        return
    if args.command == "backtest":
        requests = _load_backtest_requests(args.requests)
        data = _load_contract_data(args.data_dir)
        predictions = _backtest_predictions(data, requests)
        _write_backtest_csv(output, _result_rows(requests, predictions))
        return
    raise ValueError(f"unsupported command: {args.command}")


def main() -> None:
    args = parse_args()
    try:
        run_contract(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
