from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Iterator

import pandas as pd


FOREIGN_DAILY_CODES = frozenset(
    {
        "S0031525",
        "M0000005",
        "M0000271",
        "USDCNH0C",
        "SX5EDF0C",
        "G0003892",
        "G0006352",
        "G0006353",
        "G0003956",
        "B2559386",
        "DRS00001",
        "DRS00002",
    }
)
FOREIGN_WEEKLY_CODES = frozenset(
    {
        "HWW00001",
        "HWW00002",
        "HWW00003",
    }
)
UST_NAME_PATTERN = re.compile(
    r"(美国.*国债.*收益率|美债|u\.?s\.?\s*treasury|treasury\s+yield)",
    re.IGNORECASE,
)
_ONE_YEAR_FEATURE = re.compile(r"^([A-Za-z0-9]+)_ret\d+$")


@dataclass
class FeatureAudit:
    """Columns observed and removed from one imported model core."""

    foreign_sources_seen: set[str] = field(default_factory=set)
    candidate_columns: list[str] = field(default_factory=list)
    removed_columns: list[str] = field(default_factory=list)
    retained_columns: set[str] = field(default_factory=set)

    def record(
        self,
        *,
        candidates: list[str],
        removed: list[str],
        retained: list[str],
    ) -> None:
        self.candidate_columns.extend(
            column for column in candidates if column not in self.candidate_columns
        )
        self.removed_columns.extend(
            column for column in removed if column not in self.removed_columns
        )
        self.retained_columns.update(retained)
        self.foreign_sources_seen.update(
            source
            for column in removed
            if (source := feature_source(column)) is not None
        )


def is_us_treasury_name(value: Any) -> bool:
    return bool(UST_NAME_PATTERN.search(str(value)))


def feature_source(column: str) -> str | None:
    if column.startswith("mf_"):
        remainder = column[3:]
        return remainder.split("_", 1)[0] or None
    if column.startswith("wk_"):
        remainder = column[3:]
        return remainder.split("_", 1)[0] or None
    matched = _ONE_YEAR_FEATURE.fullmatch(column)
    return matched.group(1) if matched else None


def _filter_columns(
    frame: pd.DataFrame,
    *,
    foreign_sources: frozenset[str],
    audit: FeatureAudit,
) -> tuple[pd.DataFrame, list[str]]:
    candidates = [str(column) for column in frame.columns]
    removed = [
        column
        for column in candidates
        if feature_source(column) in foreign_sources
    ]
    retained = [column for column in candidates if column not in set(removed)]
    audit.record(candidates=candidates, removed=removed, retained=retained)
    return frame.loc[:, retained].copy(), retained


@contextmanager
def patch_core_feature_builders(
    module: ModuleType | Any,
    audit: FeatureAudit,
) -> Iterator[FeatureAudit]:
    """Temporarily remove foreign columns from V28/V31 LGBM feature builders."""

    original_mf = module.build_mf_features
    original_wkmo = module.build_wkmo_features

    def filtered_mf(*args: Any, **kwargs: Any):
        frame, categories = original_mf(*args, **kwargs)
        filtered, retained = _filter_columns(
            frame,
            foreign_sources=FOREIGN_DAILY_CODES,
            audit=audit,
        )
        retained_set = set(retained)
        filtered_categories = {
            str(column): category
            for column, category in dict(categories).items()
            if str(column) in retained_set
        }
        return filtered, filtered_categories

    def filtered_wkmo(*args: Any, **kwargs: Any):
        frame = original_wkmo(*args, **kwargs)
        filtered, _ = _filter_columns(
            frame,
            foreign_sources=FOREIGN_WEEKLY_CODES,
            audit=audit,
        )
        return filtered

    module.build_mf_features = filtered_mf
    module.build_wkmo_features = filtered_wkmo
    try:
        yield audit
    finally:
        module.build_mf_features = original_mf
        module.build_wkmo_features = original_wkmo
