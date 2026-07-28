"""日频 ledger rollout 的可信容量准入边界。

离线观测只能计算容量结论，不能直接打开 runtime。生产准入还必须满足：

* 精确绑定 policy、Mac、Registry target manifest 与候选代码/环境摘要；
* collector 对完整 evidence 做 detached CMS 签名；
* 独立 operator 对有时效且不可重放的 admission decision 做 detached CMS 签名；
* root-owned trust config 钉住两个不同的证书与当前有效 decision；
* 所有控制文件均通过防符号链接、限长、fd 前后 ``fstat`` 的安全读取。

仓库内准入记录默认保持 ``BLOCKED``；本模块不采集证据、不签名，也不自动
修改准入状态。
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Protocol

from scheduler.capacity_attestation import (
    CapacityAttestationError,
    canonical_json_bytes,
    validate_attested_capacity_evidence,
)
from scheduler.capacity_gate import (
    CapacityObservationError,
    evaluate_capacity_observations,
)
from scheduler.daily_policy import POLICY_V2_PATH
from shared.liwei_0616_cache_contract import (
    TRUSTED_CACHE_USE_QUALIFICATION_SCHEMA_VERSION,
    cache_use_qualification_sha256,
    validate_trusted_cache_use_qualification,
)


ADMISSION_SCHEMA_VERSION = "daily-capacity-admission-v2"
TRUST_SCHEMA_VERSION = "daily-capacity-cms-trust-v1"
OPERATOR_DECISION_SCHEMA_VERSION = (
    "daily-capacity-operator-decision-v1"
)
DEFAULT_ADMISSION_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "daily_capacity_admission_v2.json"
)
DEFAULT_TRUST_CONFIG_PATH = Path(
    "/Library/Application Support/BondFactorLab/"
    "capacity/trust_v1.json"
)

MAX_POLICY_BYTES = 2 * 1024 * 1024
MAX_ADMISSION_BYTES = 128 * 1024
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_SIGNATURE_BYTES = 4 * 1024 * 1024
MAX_TRUST_CONFIG_BYTES = 64 * 1024
MAX_ADMISSION_VALIDITY = timedelta(days=31)

_ADMISSION_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "decision_id",
        "decision_sequence",
        "candidate_fingerprint",
        "policy_version",
        "policy_sha256",
        "machine_id",
        "evidence_uri",
        "evidence_sha256",
        "collector_signature_uri",
        "operator_signature_uri",
        "issued_at",
        "not_before",
        "expires_at",
        "admitted_by",
        "reason",
    }
)
_BLOCKED_NULL_FIELDS = frozenset(
    {
        "decision_id",
        "decision_sequence",
        "candidate_fingerprint",
        "machine_id",
        "evidence_uri",
        "evidence_sha256",
        "collector_signature_uri",
        "operator_signature_uri",
        "issued_at",
        "not_before",
        "expires_at",
        "admitted_by",
    }
)
_TRUST_FIELDS = frozenset(
    {
        "schema_version",
        "active_decision_id",
        "minimum_decision_sequence",
        "collector",
        "operator",
    }
)
_TRUST_ROLE_FIELDS = frozenset(
    {"certificate_sha256", "keychain_uri"}
)
_OPERATOR_DECISION_FIELDS = (
    "decision_id",
    "decision_sequence",
    "candidate_fingerprint",
    "policy_version",
    "policy_sha256",
    "machine_id",
    "evidence_uri",
    "evidence_sha256",
    "issued_at",
    "not_before",
    "expires_at",
    "admitted_by",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SECURITY = Path("/usr/bin/security")
_OPENSSL = Path("/usr/bin/openssl")


class CapacityAdmissionError(RuntimeError):
    """容量证据不允许当前 Mac 启动 ledger 日批。"""


@dataclass(frozen=True)
class CmsVerificationResult:
    """CMS 校验得到的角色与签名者证书摘要。"""

    role: str
    signer_sha256: str


class CmsVerifier(Protocol):
    """可注入的 detached CMS 校验边界。"""

    def verify_detached(
        self,
        *,
        role: str,
        content: bytes,
        signature: bytes,
        trust_role: object,
    ) -> object:
        """按独立角色信任配置验证 detached CMS。"""


class MacOSCmsVerifier:
    """使用 macOS ``security cms`` 和专用 root-owned keychain 校验 CMS。

    每个角色的 keychain 必须只包含 trust config 钉住的一个证书。这样
    collector 与 operator 具有独立信任域；runtime 只持有公有信任材料，
    不持有任何可伪造 admission 的共享密钥。
    """

    def verify_detached(
        self,
        *,
        role: str,
        content: bytes,
        signature: bytes,
        trust_role: object,
    ) -> CmsVerificationResult:
        role_config = _validate_trust_role(trust_role, f"trust.{role}")
        expected_sha = str(role_config["certificate_sha256"])
        keychain = Path(str(role_config["keychain_uri"]))
        if not keychain.is_absolute():
            raise CapacityAdmissionError(
                f"{role} trust keychain_uri must be absolute"
            )
        _require_secure_root_path(keychain, f"{role} trust keychain")
        self._require_exact_pinned_certificate(
            role=role,
            keychain=keychain,
            expected_sha256=expected_sha,
        )

        try:
            with tempfile.TemporaryDirectory(
                prefix=f"bond-capacity-{role}-"
            ) as directory:
                work = Path(directory)
                content_path = work / "content.bin"
                signature_path = work / "signature.cms"
                signer_path = work / "signer.pem"
                content_path.write_bytes(content)
                signature_path.write_bytes(signature)
                content_path.chmod(0o600)
                signature_path.chmod(0o600)
                command = [
                    str(_SECURITY),
                    "cms",
                    "-D",
                    "-i",
                    str(signature_path),
                    "-c",
                    str(content_path),
                    "-k",
                    str(keychain),
                    "-u",
                    "9",
                    "-o",
                    os.devnull,
                ]
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                    timeout=30,
                    check=False,
                )
                if completed.returncode != 0:
                    raise CapacityAdmissionError(
                        f"{role} CMS signature is invalid"
                    )
                signer_result = subprocess.run(
                    [
                        str(_OPENSSL),
                        "cms",
                        "-verify",
                        "-binary",
                        "-inform",
                        "DER",
                        "-in",
                        str(signature_path),
                        "-content",
                        str(content_path),
                        "-noverify",
                        "-signer",
                        str(signer_path),
                        "-out",
                        os.devnull,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                    timeout=30,
                    check=False,
                )
                if signer_result.returncode != 0:
                    raise CapacityAdmissionError(
                        f"{role} CMS signer certificate could not be pinned"
                    )
                try:
                    signer_pem = signer_path.read_bytes()
                except OSError as exc:
                    raise CapacityAdmissionError(
                        f"{role} CMS signer certificate is unavailable"
                    ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise CapacityAdmissionError(
                f"{role} CMS verification could not run"
            ) from exc
        actual_signer_sha = _embedded_signer_sha256(
            signer_pem,
            role=role,
        )
        if not secrets.compare_digest(actual_signer_sha, expected_sha):
            raise CapacityAdmissionError(
                f"{role} CMS signer does not match pinned trust"
            )
        return CmsVerificationResult(
            role=role,
            signer_sha256=actual_signer_sha,
        )

    @staticmethod
    def _require_exact_pinned_certificate(
        *,
        role: str,
        keychain: Path,
        expected_sha256: str,
    ) -> None:
        try:
            completed = subprocess.run(
                [
                    str(_SECURITY),
                    "find-certificate",
                    "-a",
                    "-Z",
                    str(keychain),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CapacityAdmissionError(
                f"{role} trust keychain could not be inspected"
            ) from exc
        if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
            raise CapacityAdmissionError(
                f"{role} trust keychain could not be inspected"
            )
        hashes = {
            match.group(1).decode("ascii").lower()
            for match in re.finditer(
                rb"SHA-256 hash:\s*([0-9A-Fa-f]{64})",
                completed.stdout,
            )
        }
        if hashes != {expected_sha256}:
            raise CapacityAdmissionError(
                f"{role} trust keychain does not contain exactly "
                "the pinned certificate"
            )


def require_daily_capacity_admission(
    *,
    policy_path: str | Path = POLICY_V2_PATH,
    admission_path: str | Path = DEFAULT_ADMISSION_PATH,
    trust_config_path: str | Path = DEFAULT_TRUST_CONFIG_PATH,
    expected_machine_id: str | None = None,
    cms_verifier: CmsVerifier | None = None,
    now: datetime | None = None,
) -> Mapping[str, object]:
    """验证 policy、双 CMS、候选绑定和离线容量结论。

    任一不一致都 fail-closed。特别地，裸
    ``daily-capacity-observations-v2`` 即使统计结论 PASS，也不能形成
    runtime ``ADMITTED``。
    """

    policy_file = Path(policy_path)
    admission_file = Path(admission_path)
    policy_bytes = _read_control_file(
        policy_file,
        "policy",
        max_bytes=MAX_POLICY_BYTES,
    )
    admission_bytes = _read_control_file(
        admission_file,
        "capacity admission",
        max_bytes=MAX_ADMISSION_BYTES,
    )
    policy = _parse_object(policy_bytes, "policy")
    admission = _parse_object(admission_bytes, "capacity admission")
    _require_exact_fields(
        admission,
        _ADMISSION_FIELDS,
        "capacity admission",
    )
    if admission.get("schema_version") != ADMISSION_SCHEMA_VERSION:
        raise CapacityAdmissionError(
            "capacity admission schema_version is invalid"
        )

    policy_version = _required_text(
        policy.get("version"),
        "policy.version",
    )
    admitted_policy_version = _required_text(
        admission.get("policy_version"),
        "capacity admission policy_version",
    )
    if admitted_policy_version != policy_version:
        raise CapacityAdmissionError(
            "capacity admission policy_version mismatch"
        )
    actual_policy_sha = hashlib.sha256(policy_bytes).hexdigest()
    admitted_policy_sha = _required_sha256(
        admission.get("policy_sha256"),
        "capacity admission policy_sha256",
    )
    if not secrets.compare_digest(actual_policy_sha, admitted_policy_sha):
        raise CapacityAdmissionError(
            "capacity admission policy_sha256 mismatch"
        )

    status = _required_text(
        admission.get("status"),
        "capacity admission status",
    )
    if status == "BLOCKED":
        non_null = sorted(
            field
            for field in _BLOCKED_NULL_FIELDS
            if admission.get(field) is not None
        )
        if non_null:
            raise CapacityAdmissionError(
                "BLOCKED capacity admission retains active fields: "
                + ",".join(non_null)
            )
        reason = _required_text(
            admission.get("reason"),
            "capacity admission reason",
        )
        raise CapacityAdmissionError(
            f"daily capacity admission is BLOCKED: {reason}"
        )
    if status != "ADMITTED":
        raise CapacityAdmissionError(
            "capacity admission status must be BLOCKED or ADMITTED"
        )
    if admission.get("reason") is not None:
        raise CapacityAdmissionError(
            "ADMITTED capacity admission cannot retain a block reason"
        )

    # 不支持 immutable generation 的 Native 必须在读取 trust/signature 前阻断。
    _reject_unsupported_native(policy)

    machine_id = expected_machine_id or _current_machine_id()
    admitted_machine = _required_text(
        admission.get("machine_id"),
        "capacity admission machine_id",
    )
    if admitted_machine != machine_id:
        raise CapacityAdmissionError(
            "capacity admission machine_id mismatch"
        )
    decision_id = _required_text(
        admission.get("decision_id"),
        "capacity admission decision_id",
    )
    decision_sequence = _required_positive_int(
        admission.get("decision_sequence"),
        "capacity admission decision_sequence",
    )
    admitted_candidate_fingerprint = _required_sha256(
        admission.get("candidate_fingerprint"),
        "capacity admission candidate_fingerprint",
    )
    admitted_by = _required_text(
        admission.get("admitted_by"),
        "capacity admission admitted_by",
    )
    issued_at, not_before, expires_at, checked_at = _validate_time_window(
        admission,
        now=now,
    )

    trust = _load_root_trust_config(Path(trust_config_path))
    trust = _validate_trust_config(trust)
    if trust["active_decision_id"] != decision_id:
        raise CapacityAdmissionError(
            "capacity admission is not the active decision"
        )
    minimum_sequence = int(trust["minimum_decision_sequence"])
    if decision_sequence < minimum_sequence:
        raise CapacityAdmissionError(
            "capacity admission decision_sequence is below "
            "the trusted replay floor"
        )

    evidence_path = _required_absolute_path(
        admission.get("evidence_uri"),
        "capacity admission evidence_uri",
    )
    collector_signature_path = _required_absolute_path(
        admission.get("collector_signature_uri"),
        "capacity admission collector_signature_uri",
    )
    operator_signature_path = _required_absolute_path(
        admission.get("operator_signature_uri"),
        "capacity admission operator_signature_uri",
    )
    evidence_bytes = _read_control_file(
        evidence_path,
        "capacity evidence",
        max_bytes=MAX_EVIDENCE_BYTES,
    )
    expected_evidence_sha = _required_sha256(
        admission.get("evidence_sha256"),
        "capacity admission evidence_sha256",
    )
    actual_evidence_sha = hashlib.sha256(evidence_bytes).hexdigest()
    if not secrets.compare_digest(
        actual_evidence_sha,
        expected_evidence_sha,
    ):
        raise CapacityAdmissionError(
            "capacity admission evidence_sha256 mismatch"
        )
    collector_signature = _read_control_file(
        collector_signature_path,
        "collector CMS signature",
        max_bytes=MAX_SIGNATURE_BYTES,
    )
    operator_signature = _read_control_file(
        operator_signature_path,
        "operator CMS signature",
        max_bytes=MAX_SIGNATURE_BYTES,
    )

    verifier = cms_verifier or MacOSCmsVerifier()
    collector_result = _verify_signature(
        verifier,
        role="collector",
        content=evidence_bytes,
        signature=collector_signature,
        trust_role=trust["collector"],
    )
    operator_content = operator_decision_payload(admission)
    operator_result = _verify_signature(
        verifier,
        role="operator",
        content=operator_content,
        signature=operator_signature,
        trust_role=trust["operator"],
    )
    if collector_result.signer_sha256 == operator_result.signer_sha256:
        raise CapacityAdmissionError(
            "collector and operator CMS signers must be independent"
        )

    evidence = _parse_object(evidence_bytes, "capacity attested evidence")
    try:
        validated = validate_attested_capacity_evidence(
            evidence,
            expected_machine_id=machine_id,
            expected_policy_version=policy_version,
            expected_policy_sha256=actual_policy_sha,
        )
    except CapacityAttestationError as exc:
        raise CapacityAdmissionError(
            f"capacity attested evidence is invalid: {exc}"
        ) from exc
    if not secrets.compare_digest(
        validated.candidate_fingerprint,
        admitted_candidate_fingerprint,
    ):
        raise CapacityAdmissionError(
            "capacity admission candidate_fingerprint mismatch"
        )
    try:
        result = evaluate_capacity_observations(
            validated.observations,
            expected_machine_id=machine_id,
            expected_policy_version=policy_version,
        )
    except CapacityObservationError as exc:
        raise CapacityAdmissionError(
            f"capacity observations are invalid: {exc}"
        ) from exc
    if result.get("status") != "PASS":
        codes = [
            str(item.get("code"))
            for item in result.get("violations", [])
            if isinstance(item, Mapping)
        ]
        raise CapacityAdmissionError(
            "capacity evidence did not pass: "
            + ",".join(codes[:20])
        )
    trusted_cache_qualifications: dict[
        str, Mapping[str, object]
    ] = {}
    for base_scheme_id, qualification in sorted(
        validated.cache_use_qualifications.items()
    ):
        trusted = {
            "schema_version":
                TRUSTED_CACHE_USE_QUALIFICATION_SCHEMA_VERSION,
            "qualification": dict(qualification),
            "qualification_sha256":
                cache_use_qualification_sha256(qualification),
            "admission_decision_id": decision_id,
            "admission_evidence_sha256": actual_evidence_sha,
            "collector_signer_sha256":
                collector_result.signer_sha256,
            "operator_signer_sha256":
                operator_result.signer_sha256,
            "candidate_fingerprint":
                validated.candidate_fingerprint,
        }
        try:
            trusted_cache_qualifications[base_scheme_id] = (
                validate_trusted_cache_use_qualification(
                    trusted,
                    expected_base_scheme_id=base_scheme_id,
                    expected_candidate_fingerprint=(
                        validated.candidate_fingerprint
                    ),
                )
            )
        except ValueError as exc:
            raise CapacityAdmissionError(
                "signed cache qualification could not form a trusted "
                f"envelope for {base_scheme_id}: {exc}"
            ) from exc

    return {
        "schema_version": ADMISSION_SCHEMA_VERSION,
        "status": "ADMITTED",
        "decision_id": decision_id,
        "decision_sequence": decision_sequence,
        "candidate_fingerprint": validated.candidate_fingerprint,
        "candidate": dict(validated.candidate),
        "policy_version": policy_version,
        "policy_sha256": actual_policy_sha,
        "machine_id": machine_id,
        "evidence_uri": str(evidence_path),
        "evidence_sha256": actual_evidence_sha,
        "collector_signer_sha256": collector_result.signer_sha256,
        "operator_signer_sha256": operator_result.signer_sha256,
        "issued_at": issued_at.isoformat(),
        "not_before": not_before.isoformat(),
        "expires_at": expires_at.isoformat(),
        "checked_at": checked_at.isoformat(),
        "admitted_by": admitted_by,
        "cache_use_qualifications": trusted_cache_qualifications,
    }


def operator_decision_payload(
    admission: Mapping[str, object],
) -> bytes:
    """生成 operator 必须签名的不可变 decision canonical JSON。"""

    payload: dict[str, object] = {
        "schema_version": OPERATOR_DECISION_SCHEMA_VERSION,
        "status": "ADMITTED",
    }
    for field in _OPERATOR_DECISION_FIELDS:
        if field not in admission:
            raise CapacityAdmissionError(
                f"operator decision is missing {field}"
            )
        payload[field] = admission[field]
    try:
        return canonical_json_bytes(payload)
    except CapacityAttestationError as exc:
        raise CapacityAdmissionError(
            "operator decision is not canonical JSON data"
        ) from exc


def _load_root_trust_config(path: Path) -> dict[str, object]:
    raw = _read_control_file(
        path,
        "capacity trust config",
        max_bytes=MAX_TRUST_CONFIG_BYTES,
        required_owner_uids=frozenset({0}),
        owner_error="capacity trust config must be root-owned",
    )
    _require_secure_parent_chain(path, "capacity trust config")
    return _parse_object(raw, "capacity trust config")


def _validate_trust_config(
    raw: Mapping[str, object],
) -> dict[str, object]:
    trust = dict(raw)
    _require_exact_fields(trust, _TRUST_FIELDS, "capacity trust config")
    if trust.get("schema_version") != TRUST_SCHEMA_VERSION:
        raise CapacityAdmissionError(
            "capacity trust config schema_version is invalid"
        )
    trust["active_decision_id"] = _required_text(
        trust.get("active_decision_id"),
        "capacity trust config active_decision_id",
    )
    trust["minimum_decision_sequence"] = _required_positive_int(
        trust.get("minimum_decision_sequence"),
        "capacity trust config minimum_decision_sequence",
    )
    collector = _validate_trust_role(
        trust.get("collector"),
        "capacity trust config collector",
    )
    operator = _validate_trust_role(
        trust.get("operator"),
        "capacity trust config operator",
    )
    if collector["certificate_sha256"] == operator["certificate_sha256"]:
        raise CapacityAdmissionError(
            "collector and operator trust certificates must be independent"
        )
    if collector["keychain_uri"] == operator["keychain_uri"]:
        raise CapacityAdmissionError(
            "collector and operator trust keychains must be independent"
        )
    trust["collector"] = collector
    trust["operator"] = operator
    return trust


def _validate_trust_role(
    raw: object,
    label: str,
) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise CapacityAdmissionError(f"{label} must be an object")
    role = dict(raw)
    _require_exact_fields(role, _TRUST_ROLE_FIELDS, label)
    certificate_sha256 = _required_sha256(
        role.get("certificate_sha256"),
        f"{label} certificate_sha256",
    )
    keychain_uri = _required_text(
        role.get("keychain_uri"),
        f"{label} keychain_uri",
    )
    if not Path(keychain_uri).is_absolute():
        raise CapacityAdmissionError(
            f"{label} keychain_uri must be absolute"
        )
    return {
        "certificate_sha256": certificate_sha256,
        "keychain_uri": keychain_uri,
    }


def _verify_signature(
    verifier: CmsVerifier,
    *,
    role: str,
    content: bytes,
    signature: bytes,
    trust_role: object,
) -> CmsVerificationResult:
    try:
        raw = verifier.verify_detached(
            role=role,
            content=content,
            signature=signature,
            trust_role=trust_role,
        )
    except CapacityAdmissionError:
        raise
    except Exception as exc:
        raise CapacityAdmissionError(
            f"{role} CMS verification failed"
        ) from exc
    result_role = getattr(raw, "role", None)
    signer_sha256 = getattr(raw, "signer_sha256", None)
    if result_role != role:
        raise CapacityAdmissionError(
            f"{role} CMS verifier returned the wrong role"
        )
    normalized_signer = _required_sha256(
        signer_sha256,
        f"{role} CMS signer_sha256",
    )
    expected_signer = str(
        _validate_trust_role(
            trust_role,
            f"capacity trust config {role}",
        )["certificate_sha256"]
    )
    if not secrets.compare_digest(normalized_signer, expected_signer):
        raise CapacityAdmissionError(
            f"{role} CMS signer does not match pinned trust"
        )
    return CmsVerificationResult(
        role=role,
        signer_sha256=normalized_signer,
    )


def _embedded_signer_sha256(
    signer_pem: bytes,
    *,
    role: str,
) -> str:
    """从 OpenSSL 已验证的 signer 输出中提取唯一 X.509 DER 摘要。"""

    if len(signer_pem) > 1024 * 1024:
        raise CapacityAdmissionError(
            f"{role} CMS signer certificate exceeds the size limit"
        )
    blocks = re.findall(
        rb"-----BEGIN CERTIFICATE-----\s*"
        rb"([A-Za-z0-9+/=\r\n]+?)"
        rb"\s*-----END CERTIFICATE-----",
        signer_pem,
    )
    if len(blocks) != 1:
        raise CapacityAdmissionError(
            f"{role} CMS must contain exactly one signer certificate"
        )
    try:
        der = base64.b64decode(
            re.sub(rb"\s+", b"", blocks[0]),
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise CapacityAdmissionError(
            f"{role} CMS signer certificate is invalid"
        ) from exc
    if not der:
        raise CapacityAdmissionError(
            f"{role} CMS signer certificate is invalid"
        )
    return hashlib.sha256(der).hexdigest()


def _validate_time_window(
    admission: Mapping[str, object],
    *,
    now: datetime | None,
) -> tuple[datetime, datetime, datetime, datetime]:
    issued_at = _require_aware_timestamp(
        admission.get("issued_at"),
        "capacity admission issued_at",
    ).astimezone(timezone.utc)
    not_before = _require_aware_timestamp(
        admission.get("not_before"),
        "capacity admission not_before",
    ).astimezone(timezone.utc)
    expires_at = _require_aware_timestamp(
        admission.get("expires_at"),
        "capacity admission expires_at",
    ).astimezone(timezone.utc)
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise CapacityAdmissionError("capacity admission now must be aware")
    checked_at = checked_at.astimezone(timezone.utc)
    if issued_at > checked_at:
        raise CapacityAdmissionError(
            "capacity admission issued_at is in the future"
        )
    if not issued_at <= not_before < expires_at:
        raise CapacityAdmissionError(
            "capacity admission time ordering is invalid"
        )
    if expires_at - issued_at > MAX_ADMISSION_VALIDITY:
        raise CapacityAdmissionError(
            "capacity admission validity exceeds the allowed maximum"
        )
    if checked_at < not_before:
        raise CapacityAdmissionError(
            "capacity admission is not active yet"
        )
    if checked_at >= expires_at:
        raise CapacityAdmissionError("capacity admission has expired")
    return issued_at, not_before, expires_at, checked_at


def _reject_unsupported_native(policy: Mapping[str, object]) -> None:
    schemes = policy.get("schemes")
    if not isinstance(schemes, list):
        raise CapacityAdmissionError("policy.schemes must be a list")
    invalid_rows = [
        index
        for index, row in enumerate(schemes)
        if not isinstance(row, Mapping)
    ]
    if invalid_rows:
        raise CapacityAdmissionError(
            "policy.schemes must contain only objects"
        )
    unsupported = sorted(
        _required_text(
            row.get("scheme_id"),
            f"policy.schemes[{index}].scheme_id",
        )
        for index, row in enumerate(schemes)
        if row.get("input_compatibility") == "unsupported"
    )
    if unsupported:
        raise CapacityAdmissionError(
            "capacity admission cannot include unsupported Native inputs: "
            + ",".join(unsupported)
        )


def _current_machine_id() -> str:
    """读取 Mac IOPlatformUUID；无法精确绑定时拒绝降级为 hostname。"""

    try:
        completed = subprocess.run(
            [
                "/usr/sbin/ioreg",
                "-rd1",
                "-c",
                "IOPlatformExpertDevice",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin:/usr/sbin"},
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CapacityAdmissionError(
            "Mac machine identity could not be read"
        ) from exc
    if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
        raise CapacityAdmissionError(
            "Mac machine identity could not be read"
        )
    match = re.search(
        rb'"IOPlatformUUID"\s*=\s*"([^"]+)"',
        completed.stdout,
    )
    if match is None:
        raise CapacityAdmissionError(
            "Mac machine identity could not be read"
        )
    try:
        return match.group(1).decode("ascii").strip()
    except UnicodeError as exc:
        raise CapacityAdmissionError(
            "Mac machine identity is invalid"
        ) from exc


def _read_control_file(
    path: Path,
    label: str,
    *,
    max_bytes: int,
    required_owner_uids: frozenset[int] | None = None,
    owner_error: str | None = None,
) -> bytes:
    """通过单一 fd 安全读取控制文件并检测路径与内容竞态。"""

    if isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if nofollow is None or cloexec is None:
        raise CapacityAdmissionError(
            f"{label} cannot be read safely on this platform"
        )
    flags = os.O_RDONLY | nofollow | cloexec
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise CapacityAdmissionError(
                f"{label} file cannot be a symlink"
            ) from exc
        raise CapacityAdmissionError(
            f"{label} file is unavailable"
        ) from exc
    try:
        before = os.fstat(descriptor)
        _validate_open_file(
            before,
            label,
            max_bytes=max_bytes,
            required_owner_uids=required_owner_uids,
            owner_error=owner_error,
        )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise CapacityAdmissionError(
                    f"{label} file exceeds the size limit"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(after):
            raise CapacityAdmissionError(
                f"{label} file changed while being read"
            )
        return b"".join(chunks)
    except CapacityAdmissionError:
        raise
    except OSError as exc:
        raise CapacityAdmissionError(
            f"{label} file could not be read"
        ) from exc
    finally:
        os.close(descriptor)


def _validate_open_file(
    details: object,
    label: str,
    *,
    max_bytes: int,
    required_owner_uids: frozenset[int] | None,
    owner_error: str | None,
) -> None:
    mode = int(getattr(details, "st_mode"))
    owner_uid = int(getattr(details, "st_uid"))
    size = int(getattr(details, "st_size"))
    if not stat.S_ISREG(mode):
        raise CapacityAdmissionError(
            f"{label} path must be a regular file"
        )
    allowed_uids = (
        required_owner_uids
        if required_owner_uids is not None
        else frozenset({0, os.getuid()})
    )
    if owner_uid not in allowed_uids:
        raise CapacityAdmissionError(
            owner_error
            or f"{label} file is not owned by a trusted scheduler user"
        )
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise CapacityAdmissionError(
            f"{label} file cannot be group/world writable"
        )
    if size < 0 or size > max_bytes:
        raise CapacityAdmissionError(
            f"{label} file exceeds the size limit"
        )


def _file_identity(details: object) -> tuple[int, ...]:
    return (
        int(getattr(details, "st_dev")),
        int(getattr(details, "st_ino")),
        int(getattr(details, "st_size")),
        int(getattr(details, "st_mtime_ns")),
        int(getattr(details, "st_ctime_ns")),
        int(getattr(details, "st_mode")),
        int(getattr(details, "st_uid")),
    )


def _require_secure_root_path(path: Path, label: str) -> None:
    _read_control_file(
        path,
        label,
        max_bytes=MAX_SIGNATURE_BYTES,
        required_owner_uids=frozenset({0}),
        owner_error=f"{label} must be root-owned",
    )
    _require_secure_parent_chain(path, label)


def _require_secure_parent_chain(path: Path, label: str) -> None:
    current = path.absolute().parent
    while True:
        try:
            details = current.lstat()
        except OSError as exc:
            raise CapacityAdmissionError(
                f"{label} parent directory is unavailable"
            ) from exc
        if stat.S_ISLNK(details.st_mode):
            raise CapacityAdmissionError(
                f"{label} parent directory cannot be a symlink"
            )
        if not stat.S_ISDIR(details.st_mode):
            raise CapacityAdmissionError(
                f"{label} parent path must be a directory"
            )
        if details.st_uid != 0:
            raise CapacityAdmissionError(
                f"{label} parent directory must be root-owned"
            )
        if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise CapacityAdmissionError(
                f"{label} parent directory cannot be group/world writable"
            )
        if current.parent == current:
            break
        current = current.parent


def _required_absolute_path(value: object, field: str) -> Path:
    normalized = _required_text(value, field)
    path = Path(normalized)
    if not path.is_absolute():
        raise CapacityAdmissionError(f"{field} must be absolute")
    return path


def _parse_object(raw: bytes, label: str) -> dict[str, object]:
    def reject_duplicates(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CapacityAdmissionError(
                    f"{label} contains duplicate JSON key: {key}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CapacityAdmissionError(
            f"{label} is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise CapacityAdmissionError(f"{label} root must be an object")
    return value


def _require_exact_fields(
    row: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    missing = expected - set(row)
    unknown = set(row) - expected
    if missing or unknown:
        raise CapacityAdmissionError(
            f"{label} fields mismatch: "
            f"missing={sorted(missing)} unknown={sorted(unknown)}"
        )


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapacityAdmissionError(f"{field} must be non-empty text")
    return value.strip()


def _required_sha256(value: object, field: str) -> str:
    normalized = _required_text(value, field)
    if _SHA256_RE.fullmatch(normalized) is None:
        raise CapacityAdmissionError(f"{field} must be lowercase SHA-256")
    return normalized


def _required_positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CapacityAdmissionError(f"{field} must be a positive integer")
    return value


def _require_aware_timestamp(value: object, field: str) -> datetime:
    normalized = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapacityAdmissionError(
            f"{field} must be an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CapacityAdmissionError(
            f"{field} must include a UTC offset"
        )
    return parsed
