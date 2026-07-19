from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.blackbox_v2_runner import run_blackbox_predict
from shared.blackbox_v2.contracts import BlackboxMetadata, BlackboxRequest


PROBE_SCRIPT = r'''
import argparse
import json
import socket
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--request", required=True)
parser.add_argument("--data-dir", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()

network_denied = False
try:
    client = socket.socket()
    client.settimeout(1)
    client.connect(("127.0.0.1", 3306))
except PermissionError:
    network_denied = True
except OSError:
    network_denied = False

data_write_denied = False
try:
    with open(Path(args.data_dir) / "daily_output.csv", "a", encoding="utf-8") as handle:
        handle.write("forbidden")
except PermissionError:
    data_write_denied = True

if not network_denied or not data_write_denied:
    print(
        f"network_denied={network_denied}, data_write_denied={data_write_denied}",
        file=sys.stderr,
    )
    raise SystemExit(9)

with open(args.request, encoding="utf-8") as handle:
    request = json.load(handle)
result = {key: request[key] for key in ("request_id", "predict_date", "feature_date", "target_date")}
result["predicted_direction"] = 1
with open(args.output, "w", encoding="utf-8") as handle:
    json.dump(result, handle)
'''


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="blackbox-v2-sandbox-probe-") as tmpdir:
        root = Path(tmpdir)
        script = root / "sandbox_probe.py"
        script.write_text(PROBE_SCRIPT, encoding="utf-8")
        data_dir = root / "data"
        data_dir.mkdir()
        for filename in ("daily_output.csv", "weekly_output.csv", "monthly_output.csv"):
            (data_dir / filename).write_text("key,value\n1,1\n", encoding="utf-8")
        metadata = BlackboxMetadata(
            schema_version="1.0",
            scheme_id="sandbox_probe",
            name="Sandbox Probe",
            algorithm_version="1.0.0",
            target_tenor="10Y",
            task_type="T+1",
            horizon=1,
            target_rule="target_date_yield_vs_feature_date_yield",
            frequency="daily",
        )
        request = BlackboxRequest(
            request_id="sandbox-probe",
            predict_date="2026-07-16",
            feature_date="2026-07-15",
            target_date="2026-07-16",
            daily_cutoff_key="2026-07-15",
            weekly_cutoff_key="202627",
            monthly_cutoff_key="202606",
        )
        record = run_blackbox_predict(
            metadata=metadata,
            script_path=script,
            request=request,
            data_dir=data_dir,
            data_snapshot_id="sandbox-probe",
        )
    print(
        json.dumps(
            {
                "ok": record.predicted_direction == 1,
                "network_access": "denied",
                "data_dir_write": "denied",
                "runtime_profile": record.extra["runtime_profile"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
