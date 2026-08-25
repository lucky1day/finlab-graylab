from __future__ import annotations

from pathlib import Path

import pytest

from harness.contracts import import_rules


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _formatted_violations(root: Path) -> list[str]:
    return [
        violation.format(root)
        for violation in import_rules.repository_layer_import_violations(root)
    ]


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (
            {
                "shared/allowed.py": "from shared.models import PredictionRecord\n",
                "shared/bad.py": "\nfrom scheduler.executor import execute_scheme\n",
                "scheduler/scheme_edge.py": "from schemes.alpha.predict import run\n",
                "scheduler/harness_edge.py": "import harness.contracts.config_schema\n",
            },
            [
                "scheduler/harness_edge.py:1: forbidden layer import: "
                "scheduler -> harness.contracts.config_schema",
                "scheduler/scheme_edge.py:1: forbidden layer import: "
                "scheduler -> schemes.alpha.predict",
                "shared/bad.py:2: forbidden layer import: shared -> scheduler.executor",
            ],
        ),
        (
            {
                "backend/allowed.py": (
                    "from shared.models import PredictionRecord\n"
                    "from scheduler.repository import create_engine_from_env\n"
                ),
                "backend/bad.py": "from backtests.repository import clean_json\n",
                "backtests/allowed.py": (
                    "from schemes.alpha.core.model import predict\n"
                    "from schemes.alpha.inference import run_window\n"
                ),
                "backtests/bad.py": (
                    "from scheduler.executor import execute_scheme\n"
                    "from schemes.alpha.predict import run\n"
                    "import harness\n"
                ),
            },
            [
                "backend/bad.py:1: forbidden layer import: backend -> backtests.repository",
                "backtests/bad.py:1: forbidden layer import: backtests -> scheduler.executor",
                "backtests/bad.py:2: forbidden layer import: backtests -> schemes.alpha.predict",
                "backtests/bad.py:3: forbidden layer import: backtests -> harness",
            ],
        ),
        (
            {
                "schemes/alpha/core/model.py": (
                    "from shared.models import PredictionRecord\n"
                ),
                "schemes/alpha/predict.py": (
                    "from scheduler.executor import execute_scheme\n"
                    "from schemes.beta.core.model import predict\n"
                ),
            },
            [
                "schemes/alpha/core/model.py:1: forbidden layer import: "
                "schemes.alpha -> shared.models",
                "schemes/alpha/predict.py:1: forbidden layer import: "
                "schemes.alpha -> scheduler.executor",
                "schemes/alpha/predict.py:2: forbidden layer import: "
                "schemes.alpha -> schemes.beta.core.model",
            ],
        ),
        (
            {
                "schemes/alpha/predict.py": (
                    "from .core.model import predict\n"
                    "from ..beta.core import model\n"
                ),
                "schemes/alpha/core/model.py": (
                    "from .helpers import build_features\n"
                ),
            },
            [
                "schemes/alpha/predict.py:2: forbidden layer import: "
                "schemes.alpha -> schemes.beta.core",
            ],
        ),
    ],
    ids=("shared-scheduler", "backend-backtests", "native-schemes", "relative"),
)
def test_repository_layer_import_rules(
    tmp_path: Path,
    files: dict[str, str],
    expected: list[str],
) -> None:
    _write_tree(tmp_path, files)

    assert _formatted_violations(tmp_path) == expected


def test_scanner_excludes_nonproduction_paths(tmp_path: Path) -> None:
    _write_tree(
        tmp_path,
        {
            "tests/bad.py": "from scheduler import executor\n",
            "outputs/bad.py": "from scheduler import executor\n",
            "scripts/admin.py": "from scheduler import repository\n",
            "shared/ok.py": "from shared import models\n",
        },
    )

    assert _formatted_violations(tmp_path) == []


@pytest.mark.parametrize(
    ("layer", "source", "imported_module"),
    [
        (
            "harness",
            "from scripts.refresh_data_bridge_current import check_current\n",
            "scripts.refresh_data_bridge_current",
        ),
        (
            "scheduler",
            "from scripts.apply_migrations import apply_migration_files\n",
            "scripts.apply_migrations",
        ),
    ],
    ids=("harness", "scheduler"),
)
def test_control_layers_cannot_import_one_shot_admin_scripts(
    tmp_path: Path,
    layer: str,
    source: str,
    imported_module: str,
) -> None:
    _write_tree(tmp_path, {f"{layer}/bad.py": source})

    assert _formatted_violations(tmp_path) == [
        f"{layer}/bad.py:1: forbidden layer import: {layer} -> {imported_module}"
    ]


def test_current_repository_has_no_layer_inversions() -> None:
    project_root = Path(__file__).resolve().parents[1]

    assert _formatted_violations(project_root) == [], (
        "repo-wide production import graph contains upward dependencies"
    )
