from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


CORPUS_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "factor_lab_dashboard_v1_conformance.json"
)


def dashboard_v1_conformance_samples() -> list[dict[str, Any]]:
    """从共享语料生成 Python/JavaScript 使用的同一组 dashboard 样本。"""
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    base = corpus["base_payload"]
    samples: list[dict[str, Any]] = []
    for case in corpus["cases"]:
        payload = copy.deepcopy(base)
        for operation in case.get("operations", []):
            _apply_operation(payload, operation)
        samples.append(
            {
                "name": case["name"],
                "valid": case["valid"],
                "payload": payload,
            }
        )
    return samples


def _apply_operation(payload: Any, operation: dict[str, Any]) -> None:
    parent = payload
    path = operation["path"]
    for segment in path[:-1]:
        parent = parent[segment]
    leaf = path[-1]
    action = operation["op"]
    if action == "set":
        parent[leaf] = operation["value"]
        return
    if action == "add":
        parent[leaf] = operation["value"]
        return
    if action == "reverse":
        parent[leaf].reverse()
        return
    raise AssertionError(f"unknown conformance operation: {action!r}")
