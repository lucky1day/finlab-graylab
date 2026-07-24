#!/usr/bin/env python
"""离线评估 Mac Studio 日频 08:00 SLA 容量证据。

该命令只读取一个版本化 JSON 文件并输出 JSON 结论，不连接数据库、不执行
预测、不修改 policy，也不触发任何生产任务。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.capacity_gate import (  # noqa: E402
    OBSERVATION_SCHEMA_VERSION,
    CapacityObservationError,
    SUPPORTED_POLICY_VERSION,
    evaluate_capacity_observations,
)
from scheduler.capacity_attestation import (  # noqa: E402
    ATTESTED_EVIDENCE_SCHEMA_VERSION,
    CapacityAttestationError,
    validate_attested_capacity_evidence,
)


def run_command(
    observations_path: str | Path,
    *,
    expected_machine_id: str,
    expected_policy_version: str,
    require_approx_95_reliability: bool,
    expected_policy_sha256: str | None = None,
) -> tuple[int, dict[str, object]]:
    """读取并评估容量证据；退出码 0/1/2 表示通过/不通过/输入无效。"""

    path = Path(observations_path)
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_object_pairs,
        )
        candidate_fingerprint: str | None = None
        fault_trial_count: int | None = None
        observations = payload
        if (
            isinstance(payload, dict)
            and payload.get("schema_version")
            == ATTESTED_EVIDENCE_SCHEMA_VERSION
        ):
            validated = validate_attested_capacity_evidence(
                payload,
                expected_machine_id=expected_machine_id,
                expected_policy_version=expected_policy_version,
                expected_policy_sha256=expected_policy_sha256,
            )
            observations = validated.observations
            candidate_fingerprint = validated.candidate_fingerprint
            fault_trial_count = len(validated.fault_trials)
        result = evaluate_capacity_observations(
            observations,
            expected_machine_id=expected_machine_id,
            expected_policy_version=expected_policy_version,
            require_approx_95_reliability=(
                require_approx_95_reliability
            ),
        )
        result = {
            **result,
            **(
                {
                    "candidate_fingerprint": candidate_fingerprint,
                    "fault_trial_count": fault_trial_count,
                }
                if candidate_fingerprint is not None
                else {}
            ),
            "cms_verified": False,
            "runtime_admission_eligible": False,
        }
    except (
        CapacityAttestationError,
        CapacityObservationError,
        FileNotFoundError,
        IsADirectoryError,
        PermissionError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        return 2, {
            "status": "FAIL",
            "violations": [
                {
                    "code": "INVALID_OBSERVATIONS",
                    "message": _safe_input_error(exc),
                }
            ],
        }
    return (0 if result["status"] == "PASS" else 1), result


def _reject_duplicate_object_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CapacityObservationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _safe_input_error(exc: BaseException) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return (
            "observations file is invalid JSON "
            f"(line {exc.lineno}, column {exc.colno})"
        )
    if isinstance(exc, FileNotFoundError):
        return "observations file was not found"
    if isinstance(exc, IsADirectoryError):
        return "observations path must be a file"
    if isinstance(exc, PermissionError):
        return "observations file is not readable"
    if isinstance(exc, UnicodeError):
        return "observations file must be valid UTF-8"
    return str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "observations",
        type=Path,
        help=f"{OBSERVATION_SCHEMA_VERSION} JSON 文件",
    )
    parser.add_argument(
        "--expected-machine-id",
        default=socket.gethostname(),
        help="容量准入绑定的 Mac 标识；默认使用本机 hostname",
    )
    parser.add_argument(
        "--expected-policy-version",
        default=SUPPORTED_POLICY_VERSION,
        help="固定 scheduler policy 版本",
    )
    parser.add_argument(
        "--expected-policy-sha256",
        help="可选：attested envelope 必须绑定的实际 policy bytes SHA-256",
    )
    parser.add_argument(
        "--require-approx-95-reliability",
        action="store_true",
        help="同时申请约 95%% 零失败可靠性声明（至少 59 个有效日批）",
    )
    args = parser.parse_args(argv)
    exit_code, result = run_command(
        args.observations,
        expected_machine_id=args.expected_machine_id,
        expected_policy_version=args.expected_policy_version,
        expected_policy_sha256=args.expected_policy_sha256,
        require_approx_95_reliability=(
            args.require_approx_95_reliability
        ),
    )
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
