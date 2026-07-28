"""把已签名容量证据与当前 Mac/DB/worktree 做运行时精确绑定。"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Iterable, Mapping

from scheduler.capacity_admission import (
    CapacityAdmissionError,
    require_daily_capacity_admission,
)
from scheduler.capacity_attestation import (
    CapacityAttestationError,
    verify_current_candidate,
)
from scheduler.capacity_candidate_runtime import (
    CapacityCandidateRuntimeError,
    NATIVE_ENV_NAME,
    build_current_capacity_candidate,
)
from scheduler.daily_policy import POLICY_V2_PATH
from scheduler.discovery import SchemeConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def require_current_capacity_admission(
    engine: object,
    *,
    project_root: str | Path = PROJECT_ROOT,
    policy_path: str | Path = POLICY_V2_PATH,
    discovered: Iterable[SchemeConfig] | None = None,
    algo_env: str = NATIVE_ENV_NAME,
) -> Mapping[str, object]:
    """先验证双 CMS admission，再重算并精确比较当前 candidate。

    本入口包含 discovery、文件 rehash、conda manifest 与 DB schema 读取，
    只应在 scheduler 启动、日批协调器和 operator recovery 边界调用。
    """

    admission = require_daily_capacity_admission(
        policy_path=policy_path,
    )
    signed_candidate = admission.get("candidate")
    if not isinstance(signed_candidate, Mapping):
        raise CapacityAdmissionError(
            "capacity admission has no signed candidate"
        )
    if algo_env != NATIVE_ENV_NAME:
        raise CapacityAdmissionError(
            "ledger algorithm environment must be the signed fixed "
            f"environment {NATIVE_ENV_NAME!r}; found {algo_env!r}"
        )
    try:
        current = build_current_capacity_candidate(
            engine,
            project_root=project_root,
            policy_path=policy_path,
            discovered=discovered,
            native_env=algo_env,
        )
        verified = verify_current_candidate(
            signed_candidate,
            current.payload,
        )
    except (
        CapacityCandidateRuntimeError,
        CapacityAttestationError,
    ) as exc:
        raise CapacityAdmissionError(
            "current capacity candidate does not match signed admission"
        ) from exc

    admitted_fingerprint = admission.get("candidate_fingerprint")
    if (
        not isinstance(admitted_fingerprint, str)
        or not secrets.compare_digest(
            admitted_fingerprint,
            verified.fingerprint,
        )
    ):
        raise CapacityAdmissionError(
            "current capacity candidate fingerprint is not admitted"
        )
    return {
        **dict(admission),
        "current_candidate": dict(verified.payload),
        "current_candidate_fingerprint": verified.fingerprint,
    }
