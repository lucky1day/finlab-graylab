from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import asdict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_scheme(scheme_id: str, predict_date: str) -> list[dict]:
    """在算法环境中执行方案，并返回可 JSON 序列化的预测记录。"""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    module = importlib.import_module(f"schemes.{scheme_id}.predict")
    records = module.run(predict_date)
    return [asdict(record) for record in records]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one scheme and emit PredictionRecord JSON.")
    parser.add_argument("--scheme-id", required=True)
    parser.add_argument("--predict-date", required=True)
    args = parser.parse_args()
    payload = run_scheme(args.scheme_id, args.predict_date)
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
