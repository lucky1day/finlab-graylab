from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_capacity_gate import (
    MACHINE_ID,
    POLICY_VERSION,
    _attested_payload,
    _passing_payload,
    _sha256,
)


COLLECTOR_CERT_SHA256 = _sha256("collector-certificate")
OPERATOR_CERT_SHA256 = _sha256("operator-certificate")
DECISION_ID = "capacity-decision-20260724-0001"
DECISION_SEQUENCE = 42
NOW = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)


class FakeCmsVerifier:
    def __init__(
        self,
        *,
        collector_signer: str = COLLECTOR_CERT_SHA256,
        operator_signer: str = OPERATOR_CERT_SHA256,
    ) -> None:
        self._signers = {
            "collector": collector_signer,
            "operator": operator_signer,
        }
        self.calls: list[dict[str, object]] = []

    def verify_detached(
        self,
        *,
        role: str,
        content: bytes,
        signature: bytes,
        trust_role: object,
    ) -> object:
        self.calls.append(
            {
                "role": role,
                "content": content,
                "signature": signature,
                "trust_role": trust_role,
            }
        )
        return SimpleNamespace(
            role=role,
            signer_sha256=self._signers[role],
        )


def _trust_payload(
    *,
    collector_sha256: str = COLLECTOR_CERT_SHA256,
    operator_sha256: str = OPERATOR_CERT_SHA256,
    active_decision_id: str = DECISION_ID,
    minimum_decision_sequence: int = DECISION_SEQUENCE,
) -> dict[str, object]:
    return {
        "schema_version": "daily-capacity-cms-trust-v1",
        "active_decision_id": active_decision_id,
        "minimum_decision_sequence": minimum_decision_sequence,
        "collector": {
            "certificate_sha256": collector_sha256,
            "keychain_uri":
                "/Library/Application Support/BondFactorLab/"
                "capacity/collector-trust.keychain-db",
        },
        "operator": {
            "certificate_sha256": operator_sha256,
            "keychain_uri":
                "/Library/Application Support/BondFactorLab/"
                "capacity/operator-trust.keychain-db",
        },
    }


class DailyCapacityAdmissionTests(unittest.TestCase):
    def _write_json(
        self,
        root: Path,
        name: str,
        payload: object,
    ) -> Path:
        path = root / name
        path.write_text(
            json.dumps(payload, ensure_ascii=True, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def _write_bytes(
        self,
        root: Path,
        name: str,
        payload: bytes,
    ) -> Path:
        path = root / name
        path.write_bytes(payload)
        return path

    def _policy(
        self,
        root: Path,
        *,
        unsupported: bool = False,
    ) -> Path:
        return self._write_json(
            root,
            "policy.json",
            {
                "version": POLICY_VERSION,
                "schemes": [
                    {
                        "scheme_id": "ready",
                        "input_compatibility": (
                            "unsupported"
                            if unsupported
                            else "generation_v1"
                        ),
                    }
                ],
            },
        )

    def _admission(
        self,
        *,
        policy_path: Path,
        evidence_path: Path | None,
        collector_signature_path: Path | None,
        operator_signature_path: Path | None,
        status: str = "ADMITTED",
        machine_id: str = MACHINE_ID,
        decision_id: str = DECISION_ID,
        decision_sequence: int = DECISION_SEQUENCE,
        issued_at: str = "2026-07-24T01:00:00Z",
        not_before: str = "2026-07-24T01:05:00Z",
        expires_at: str = "2026-08-01T00:00:00Z",
        candidate_fingerprint: str | None = None,
    ) -> dict[str, object]:
        evidence_bytes = (
            evidence_path.read_bytes()
            if evidence_path is not None
            else b""
        )
        if (
            status == "ADMITTED"
            and candidate_fingerprint is None
            and evidence_path is not None
        ):
            candidate_fingerprint = json.loads(
                evidence_bytes.decode("utf-8")
            ).get("candidate_fingerprint")
        admitted = status == "ADMITTED"
        return {
            "schema_version": "daily-capacity-admission-v2",
            "status": status,
            "decision_id": decision_id if admitted else None,
            "decision_sequence": (
                decision_sequence if admitted else None
            ),
            "candidate_fingerprint": (
                candidate_fingerprint if admitted else None
            ),
            "policy_version": POLICY_VERSION,
            "policy_sha256": hashlib.sha256(
                policy_path.read_bytes()
            ).hexdigest(),
            "machine_id": machine_id if admitted else None,
            "evidence_uri": (
                str(evidence_path.absolute())
                if admitted and evidence_path is not None
                else None
            ),
            "evidence_sha256": (
                hashlib.sha256(evidence_bytes).hexdigest()
                if admitted
                else None
            ),
            "collector_signature_uri": (
                str(collector_signature_path.absolute())
                if admitted and collector_signature_path is not None
                else None
            ),
            "operator_signature_uri": (
                str(operator_signature_path.absolute())
                if admitted and operator_signature_path is not None
                else None
            ),
            "issued_at": issued_at if admitted else None,
            "not_before": not_before if admitted else None,
            "expires_at": expires_at if admitted else None,
            "admitted_by": "capacity-operator" if admitted else None,
            "reason": (
                None
                if admitted
                else "capacity evidence is incomplete"
            ),
        }

    def _bundle(
        self,
        root: Path,
        *,
        unsupported: bool = False,
        raw_observations: bool = False,
        **admission_overrides: object,
    ) -> tuple[Path, Path, bytes, bytes]:
        policy = self._policy(root, unsupported=unsupported)
        evidence_payload = (
            _passing_payload()
            if raw_observations
            else _attested_payload(
                policy_sha256=hashlib.sha256(
                    policy.read_bytes()
                ).hexdigest()
            )
        )
        evidence = self._write_json(
            root,
            "evidence.json",
            evidence_payload,
        )
        collector_signature = self._write_bytes(
            root,
            "collector.cms",
            b"collector-cms-signature",
        )
        operator_signature = self._write_bytes(
            root,
            "operator.cms",
            b"operator-cms-signature",
        )
        admission = self._write_json(
            root,
            "admission.json",
            self._admission(
                policy_path=policy,
                evidence_path=evidence,
                collector_signature_path=collector_signature,
                operator_signature_path=operator_signature,
                **admission_overrides,
            ),
        )
        return (
            policy,
            admission,
            evidence.read_bytes(),
            collector_signature.read_bytes(),
        )

    def _require_with_trust(
        self,
        *,
        policy: Path,
        admission: Path,
        verifier: FakeCmsVerifier,
        trust_payload: dict[str, object] | None = None,
        now: datetime = NOW,
    ) -> dict[str, object]:
        from scheduler.capacity_admission import (
            require_daily_capacity_admission,
        )

        with patch(
            "scheduler.capacity_admission._load_root_trust_config",
            return_value=trust_payload or _trust_payload(),
        ):
            return dict(
                require_daily_capacity_admission(
                    policy_path=policy,
                    admission_path=admission,
                    expected_machine_id=MACHINE_ID,
                    cms_verifier=verifier,
                    now=now,
                )
            )

    def test_repository_admission_is_blocked_by_default(self) -> None:
        from scheduler.capacity_admission import (
            CapacityAdmissionError,
            require_daily_capacity_admission,
        )

        with self.assertRaisesRegex(
            CapacityAdmissionError,
            "BLOCKED",
        ):
            require_daily_capacity_admission()

    def test_repository_blocked_record_binds_exact_raw_policy_bytes(
        self,
    ) -> None:
        import hashlib

        from scheduler.capacity_admission import (
            DEFAULT_ADMISSION_PATH,
        )
        from scheduler.daily_policy import POLICY_V2_PATH

        admission = json.loads(
            DEFAULT_ADMISSION_PATH.read_text(encoding="utf-8")
        )
        self.assertEqual(admission["status"], "BLOCKED")
        self.assertEqual(
            admission["policy_sha256"],
            hashlib.sha256(POLICY_V2_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            admission["policy_version"],
            "daily-scheduler-policy-v2",
        )
        for field in (
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
        ):
            self.assertIsNone(admission[field], field)

    def test_runtime_admission_requires_two_independent_cms_signatures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy, admission, evidence_bytes, collector_signature = (
                self._bundle(root)
            )
            verifier = FakeCmsVerifier()

            result = self._require_with_trust(
                policy=policy,
                admission=admission,
                verifier=verifier,
            )

        self.assertEqual(result["status"], "ADMITTED")
        self.assertEqual(result["decision_id"], DECISION_ID)
        self.assertEqual(
            result["collector_signer_sha256"],
            COLLECTOR_CERT_SHA256,
        )
        self.assertEqual(
            result["operator_signer_sha256"],
            OPERATOR_CERT_SHA256,
        )
        self.assertEqual(
            result["candidate_fingerprint"],
            json.loads(evidence_bytes.decode("utf-8"))[
                "candidate_fingerprint"
            ],
        )
        self.assertEqual(
            result["candidate"]["memory_bytes"],
            64 * 1024**3,
        )
        self.assertEqual(
            set(result["cache_use_qualifications"]),
            {f"native-{index:02d}" for index in range(10)},
        )
        first_qualification = result["cache_use_qualifications"][
            "native-00"
        ]
        self.assertEqual(
            first_qualification["admission_decision_id"],
            DECISION_ID,
        )
        self.assertEqual(
            first_qualification["admission_evidence_sha256"],
            result["evidence_sha256"],
        )
        self.assertEqual(
            [call["role"] for call in verifier.calls],
            ["collector", "operator"],
        )
        self.assertEqual(verifier.calls[0]["content"], evidence_bytes)
        self.assertEqual(
            verifier.calls[0]["signature"],
            collector_signature,
        )
        operator_payload = json.loads(
            verifier.calls[1]["content"].decode("utf-8")
        )
        self.assertEqual(operator_payload["decision_id"], DECISION_ID)
        self.assertEqual(
            operator_payload["candidate_fingerprint"],
            result["candidate_fingerprint"],
        )

    def test_raw_observations_cannot_become_runtime_admitted(
        self,
    ) -> None:
        from scheduler.capacity_admission import CapacityAdmissionError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy, admission, _, _ = self._bundle(
                root,
                raw_observations=True,
                candidate_fingerprint=_sha256("hand-written"),
            )
            verifier = FakeCmsVerifier()

            with self.assertRaisesRegex(
                CapacityAdmissionError,
                "attested evidence",
            ):
                self._require_with_trust(
                    policy=policy,
                    admission=admission,
                    verifier=verifier,
                )

    def test_missing_or_non_root_trust_config_fails_closed(self) -> None:
        from scheduler.capacity_admission import (
            CapacityAdmissionError,
            require_daily_capacity_admission,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy, admission, _, _ = self._bundle(root)
            missing = root / "missing-trust.json"
            with self.assertRaisesRegex(
                CapacityAdmissionError,
                "trust config",
            ):
                require_daily_capacity_admission(
                    policy_path=policy,
                    admission_path=admission,
                    expected_machine_id=MACHINE_ID,
                    cms_verifier=FakeCmsVerifier(),
                    trust_config_path=missing,
                    now=NOW,
                )

            untrusted = self._write_json(
                root,
                "trust.json",
                _trust_payload(),
            )
            self.assertNotEqual(os.stat(untrusted).st_uid, 0)
            with self.assertRaisesRegex(
                CapacityAdmissionError,
                "root-owned",
            ):
                require_daily_capacity_admission(
                    policy_path=policy,
                    admission_path=admission,
                    expected_machine_id=MACHINE_ID,
                    cms_verifier=FakeCmsVerifier(),
                    trust_config_path=untrusted,
                    now=NOW,
                )

    def test_collector_and_operator_must_be_different_signers(self) -> None:
        from scheduler.capacity_admission import CapacityAdmissionError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy, admission, _, _ = self._bundle(root)
            same_signer = _sha256("same-signer")
            verifier = FakeCmsVerifier(
                collector_signer=same_signer,
                operator_signer=same_signer,
            )
            trust = _trust_payload(
                collector_sha256=same_signer,
                operator_sha256=same_signer,
            )

            with self.assertRaisesRegex(
                CapacityAdmissionError,
                "independent",
            ):
                self._require_with_trust(
                    policy=policy,
                    admission=admission,
                    verifier=verifier,
                    trust_payload=trust,
                )

    def test_active_decision_and_sequence_prevent_replay(self) -> None:
        from scheduler.capacity_admission import CapacityAdmissionError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy, admission, _, _ = self._bundle(root)
            verifier = FakeCmsVerifier()

            with self.assertRaisesRegex(
                CapacityAdmissionError,
                "active decision",
            ):
                self._require_with_trust(
                    policy=policy,
                    admission=admission,
                    verifier=verifier,
                    trust_payload=_trust_payload(
                        active_decision_id="newer-decision",
                    ),
                )
            with self.assertRaisesRegex(
                CapacityAdmissionError,
                "decision_sequence",
            ):
                self._require_with_trust(
                    policy=policy,
                    admission=admission,
                    verifier=verifier,
                    trust_payload=_trust_payload(
                        minimum_decision_sequence=DECISION_SEQUENCE + 1,
                    ),
                )

    def test_admission_time_window_is_bounded_and_current(self) -> None:
        from scheduler.capacity_admission import CapacityAdmissionError

        cases = (
            (
                {"expires_at": "2026-07-24T01:59:59Z"},
                "expired",
            ),
            (
                {"not_before": "2026-07-24T02:00:01Z"},
                "not active",
            ),
            (
                {"issued_at": "2026-07-24T02:00:01Z"},
                "future",
            ),
            (
                {"expires_at": "2027-07-24T02:00:00Z"},
                "validity",
            ),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    policy, admission, _, _ = self._bundle(
                        root,
                        **overrides,
                    )
                    with self.assertRaisesRegex(
                        CapacityAdmissionError,
                        message,
                    ):
                        self._require_with_trust(
                            policy=policy,
                            admission=admission,
                            verifier=FakeCmsVerifier(),
                        )

    def test_unsupported_native_blocks_before_signature_verification(
        self,
    ) -> None:
        from scheduler.capacity_admission import CapacityAdmissionError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy, admission, _, _ = self._bundle(
                root,
                unsupported=True,
            )
            verifier = FakeCmsVerifier()

            with self.assertRaisesRegex(
                CapacityAdmissionError,
                "unsupported",
            ):
                self._require_with_trust(
                    policy=policy,
                    admission=admission,
                    verifier=verifier,
                )

        self.assertEqual(verifier.calls, [])

    def test_admission_rejects_tampered_evidence_before_cms(self) -> None:
        from scheduler.capacity_admission import CapacityAdmissionError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy, admission, _, _ = self._bundle(root)
            admission_payload = json.loads(
                admission.read_text(encoding="utf-8")
            )
            evidence = Path(admission_payload["evidence_uri"])
            evidence.write_text('{"tampered":true}', encoding="utf-8")
            verifier = FakeCmsVerifier()

            with self.assertRaisesRegex(
                CapacityAdmissionError,
                "evidence_sha256",
            ):
                self._require_with_trust(
                    policy=policy,
                    admission=admission,
                    verifier=verifier,
                )

        self.assertEqual(verifier.calls, [])

    def test_admission_rejects_symlinked_or_group_writable_evidence(
        self,
    ) -> None:
        from scheduler.capacity_admission import CapacityAdmissionError

        for unsafe_kind in ("symlink", "group_writable"):
            with self.subTest(unsafe_kind=unsafe_kind):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    policy, admission, _, _ = self._bundle(root)
                    admission_payload = json.loads(
                        admission.read_text(encoding="utf-8")
                    )
                    evidence = Path(admission_payload["evidence_uri"])
                    if unsafe_kind == "symlink":
                        real = root / "real-evidence.json"
                        evidence.rename(real)
                        evidence.symlink_to(real)
                    else:
                        evidence.chmod(0o664)
                    with self.assertRaisesRegex(
                        CapacityAdmissionError,
                        "symlink|writable",
                    ):
                        self._require_with_trust(
                            policy=policy,
                            admission=admission,
                            verifier=FakeCmsVerifier(),
                        )

    def test_secure_reader_rejects_oversize_and_mid_read_mutation(
        self,
    ) -> None:
        from scheduler import capacity_admission
        from scheduler.capacity_admission import CapacityAdmissionError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oversized = root / "oversized.json"
            oversized.write_bytes(
                b"x" * (capacity_admission.MAX_ADMISSION_BYTES + 1)
            )
            with self.assertRaisesRegex(
                CapacityAdmissionError,
                "size limit",
            ):
                capacity_admission._read_control_file(
                    oversized,
                    "capacity admission",
                    max_bytes=capacity_admission.MAX_ADMISSION_BYTES,
                )

            stable = root / "stable.json"
            stable.write_bytes(b"{}")
            actual = os.stat(stable)
            changed = SimpleNamespace(
                st_mode=actual.st_mode,
                st_uid=actual.st_uid,
                st_size=actual.st_size,
                st_dev=actual.st_dev,
                st_ino=actual.st_ino,
                st_mtime_ns=actual.st_mtime_ns + 1,
                st_ctime_ns=actual.st_ctime_ns,
            )
            with (
                patch(
                    "scheduler.capacity_admission.os.fstat",
                    side_effect=(actual, changed),
                ),
                self.assertRaisesRegex(
                    CapacityAdmissionError,
                    "changed while being read",
                ),
            ):
                capacity_admission._read_control_file(
                    stable,
                    "capacity evidence",
                    max_bytes=1024,
                )

    def test_embedded_cms_signer_is_exactly_one_pinned_certificate(
        self,
    ) -> None:
        from scheduler.capacity_admission import (
            CapacityAdmissionError,
            _embedded_signer_sha256,
        )

        der = b"test-only-certificate-der"
        pem = (
            b"-----BEGIN CERTIFICATE-----\n"
            + base64.b64encode(der)
            + b"\n-----END CERTIFICATE-----\n"
        )
        self.assertEqual(
            _embedded_signer_sha256(pem, role="collector"),
            hashlib.sha256(der).hexdigest(),
        )
        with self.assertRaisesRegex(
            CapacityAdmissionError,
            "exactly one",
        ):
            _embedded_signer_sha256(pem + pem, role="collector")


if __name__ == "__main__":
    unittest.main()
