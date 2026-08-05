from __future__ import annotations

import hashlib
from pathlib import Path

from harness import daily_real_replay_operator as operator


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_replay_preflight_accepts_exact_current_001_through_019_manifest() -> None:
    expected = operator._expected_production_migrations()

    assert operator.EXPECTED_PRODUCTION_MIGRATIONS == tuple(range(1, 20))
    assert [row[0] for row in expected] == list(range(1, 20))
    assert expected[-1] == (
        19,
        "019_retire_scheme_serving_pointer.sql",
        hashlib.sha256(
            (
                PROJECT_ROOT
                / "migrations"
                / "019_retire_scheme_serving_pointer.sql"
            ).read_bytes()
        ).hexdigest(),
        "APPLIED",
    )
