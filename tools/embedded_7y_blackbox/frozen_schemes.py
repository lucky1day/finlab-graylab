"""Approved immutable specifications for the embedded 7Y deliveries."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class AnchorIdentity:
    label: str
    source_scheme_id: str
    config_sha256: str
    runner_sha256: str


@dataclass(frozen=True)
class FrozenScheme:
    scheme_id: str
    candidate_id: str
    candidate_hash: str
    curve_family: str
    curve_member_id: str
    curve_member_hash: str
    edge_minimum_history: int
    hfas_member_id: str = "7y-hfas-10173"
    hfas_member_hash: str = "a8cbf01f35327bb7"
    member_weights: tuple[int, int] = (1, 2)
    threshold: float = 0.0
    tie_rule: str = "abstain"


DIRECT_YIELD_COLUMNS = ("TB5YWI0C", "TB7YWI0C", "TB0YWI0C")
FAIR_VALUE_WEIGHTS = (0.6, 0.4)
EDGE_WINDOW = 42
GAP_Z_MINIMUM_HISTORY = 42
GAP_Z_WINDOW = 84
CURVE_THRESHOLD_WINDOW = 252
CURVE_ACTIVE_RATE = 0.55
HFAS_ACTIVE_RATE = 0.60
HFAS_THRESHOLD_WINDOW = 42
HFAS_STATE_Z_WINDOW = 84
ANCHOR_WEIGHTS = (1.0, -1.0)

FIVE_Y10_ANCHOR = AnchorIdentity(
    label="5Y10",
    source_scheme_id="daily_5y_lgbm_5y10_0629",
    config_sha256="0bd80068ff2c287b0c679ed555bab1dc1720815fd386a4aeb77a0a9f81190e62",
    runner_sha256="695f4d6365311827e732b02c6e6a2387d4f297d8e929d209818402c36b17f1c1",
)
TEN_Y04_ANCHOR = AnchorIdentity(
    label="10Y04",
    source_scheme_id="daily_10y_lgbm_10y04_0629",
    config_sha256="587fda91c9ca85a3805c9832ad99e4f6cb80e3f39c3f1057161cf944ac7b9712",
    runner_sha256="c29c87600e017c4609b9a75a04b9310788f5bd6c113532e54861383619d8a77f",
)
ANCHOR_IDENTITIES = (FIVE_Y10_ANCHOR, TEN_Y04_ANCHOR)

SCHEMES: Mapping[str, FrozenScheme] = MappingProxyType({
    "seven_y_t1_cfc_0084_embedded_v1": FrozenScheme(
        scheme_id="seven_y_t1_cfc_0084_embedded_v1",
        candidate_id="7y-cfc-0084",
        candidate_hash="69bf3a432784639d",
        curve_family="fast_causal_curve_orientation",
        curve_member_id="7y-fcco-08756",
        curve_member_hash="e80445063865243d",
        edge_minimum_history=21,
    ),
    "seven_y_t1_cfc_0156_embedded_v1": FrozenScheme(
        scheme_id="seven_y_t1_cfc_0156_embedded_v1",
        candidate_id="7y-cfc-0156",
        candidate_hash="ad0d94dc15febd8a",
        curve_family="causal_curve_orientation",
        curve_member_id="7y-cco-03620",
        curve_member_hash="182131092906070b",
        edge_minimum_history=14,
    ),
})


def get_scheme(scheme_id: str) -> FrozenScheme:
    """Return the exact approved frozen scheme identified by ``scheme_id``."""
    return SCHEMES[scheme_id]
