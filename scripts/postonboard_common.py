from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from harness.config_loader import load_config_raw


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATUSES = {"pass", "fail", "blocked"}
EXIT_CODES = {"pass": 0, "fail": 1, "blocked": 2}


def default_output_dir(scheme_id: str, project_root: Path = PROJECT_ROOT) -> Path:
    return project_root / "reports" / "postonboard" / scheme_id


def load_scheme_config(scheme_id: str, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    config_path = project_root / "schemes" / scheme_id / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"missing scheme config: {config_path}")
    raw = load_config_raw(config_path)
    if str(raw.get("scheme_id", "")).strip() != scheme_id:
        raise ValueError(f"{config_path}: scheme_id must equal {scheme_id}")
    return raw


def make_payload(status: str, evidence: dict[str, Any] | None = None, errors: list[str] | None = None) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    return {
        "status": status,
        "evidence": evidence or {},
        "errors": errors or [],
    }


def exit_code(status: str) -> int:
    return EXIT_CODES[status]


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean_json(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(clean_json(payload), ensure_ascii=False, indent=2))


def finish(payload: dict[str, Any]) -> int:
    print_json(payload)
    return exit_code(payload["status"])


def clean_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value
