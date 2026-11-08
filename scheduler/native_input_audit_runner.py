"""仅导出和审计Native输入的独立进程入口，不执行模型。"""
from __future__ import annotations

def main() -> None:
    """独立、有界的审计子进程；不执行任何模型。"""
    from pathlib import Path
    from shared.native_prediction_input_audit import native_prediction_input_audit
    import argparse
    import json
    from scheduler.discovery import load_scheme_config
    parser = argparse.ArgumentParser()
    parser.add_argument('--scheme-id', required=True)
    parser.add_argument('--predict-date', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    cfg = load_scheme_config(root/'schemes'/args.scheme_id/'config.yaml')
    with native_prediction_input_audit(cfg, args.predict_date) as audit:
        pass
    print(json.dumps({'kind': 'native_input_audit_result', 'summary': audit.summary}, ensure_ascii=False))


if __name__ == '__main__':
    main()
