from __future__ import annotations

from pathlib import Path

import pytest

from harness.contracts import import_rules


@pytest.mark.parametrize(
    ("path", "source", "forbidden"),
    [
        ("shared/ok.py", "from shared.models import PredictionRecord", False),
        ("shared/bad.py", "from scheduler.executor import execute_scheme", True),
        ("scheduler/bad.py", "from schemes.alpha.predict import run", True),
        ("scheduler/bad.py", "import harness.contracts.config_schema", True),
        ("backend/ok.py", "from scheduler.repository import create_engine_from_env", False),
        ("backend/bad.py", "from backtests.repository import clean_json", True),
        ("backtests/ok.py", "from schemes.alpha.core.model import predict", False),
        ("backtests/bad.py", "from scheduler.executor import execute_scheme", True),
        ("backtests/bad.py", "from schemes.alpha.predict import run", True),
        ("backtests/bad.py", "import harness", True),
        ("schemes/alpha/core/bad.py", "from shared.models import PredictionRecord", True),
        ("schemes/alpha/bad.py", "from scheduler.executor import execute_scheme", True),
        ("schemes/alpha/bad.py", "from schemes.beta.core.model import predict", True),
        ("schemes/alpha/ok.py", "from .core.model import predict", False),
        ("schemes/alpha/bad.py", "from ..beta.core import model", True),
        ("schemes/alpha/core/ok.py", "from .helpers import build_features", False),
        ("harness/bad.py", "from scripts.refresh_data_bridge_current import check_current", True),
        ("scheduler/bad.py", "from scripts.apply_migrations import apply_migration_files", True),
    ],
)
def test_repository_layer_import_rules(
    tmp_path: Path, path: str, source: str, forbidden: bool,
) -> None:
    module = tmp_path / path
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(source, encoding="utf-8")

    violations = import_rules.repository_layer_import_violations(tmp_path)
    assert [violation.path for violation in violations] == ([module] if forbidden else [])


def test_scanner_excludes_nonproduction_paths(tmp_path: Path) -> None:
    for path in ("tests/bad.py", "outputs/bad.py", "scripts/admin.py"):
        module = tmp_path / path
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text("from scheduler import executor", encoding="utf-8")

    assert import_rules.repository_layer_import_violations(tmp_path) == []


def test_current_repository_has_no_layer_inversions() -> None:
    project_root = Path(__file__).resolve().parents[1]
    violations = import_rules.repository_layer_import_violations(project_root)
    assert not violations, [violation.format(project_root) for violation in violations]
