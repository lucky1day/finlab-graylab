from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus
from scheduler.repository import bootstrap_blackbox_control_plane


class BlackboxBootstrapGate(Gate):
    """在空隔离 Schema 中创建唯一 draft/paused 控制面身份。"""

    name = "bootstrap"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        cfg = ctx.config
        if cfg is None or getattr(cfg, "runtime_type", None) != "blackbox_v2":
            raise ValueError("Blackbox bootstrap requires a valid Blackbox config")
        expected_schema = str(ctx.expected_empty_schema or "").strip()
        if not expected_schema:
            return GateResult(
                gate_name=self.name,
                status=GateStatus.BLOCKED,
                passed=False,
                evidence=[Evidence("expected_empty_schema", None)],
                errors=["bootstrap requires --expected-empty-schema"],
                started_at=started_at,
                finished_at=utc_now(),
            )

        audit_path = ctx.report_dir / "bootstrap_result.json"
        _assert_audit_writable(audit_path)
        prepared_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        prepared_payload = {
            "action": "blackbox_bootstrap",
            "status": "prepared",
            "prepared_at": prepared_at,
            "schema_name": expected_schema,
            "scheme_id": cfg.scheme_id,
            "scheme_version": cfg.scheme_version,
            "source_config": str(cfg.path / "config.yaml"),
            "target_state": {"version": "draft", "registry": "paused"},
            "activation_performed": False,
            "business_writes_performed": False,
        }
        _atomic_write_json(audit_path, prepared_payload)
        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        try:
            state = bootstrap_blackbox_control_plane(
                engine,
                cfg,
                expected_schema=expected_schema,
            )
        finally:
            if hasattr(engine, "dispose"):
                engine.dispose()

        payload = {
            **prepared_payload,
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "schema_name": state.schema_name,
            "scheme_id": state.scheme_id,
            "scheme_version": state.scheme_version,
            "preflight_table_counts": state.table_counts,
            "target_state": {
                "version": state.version_status,
                "registry": state.registry_status,
            },
        }
        _atomic_write_json(audit_path, payload)
        return GateResult(
            gate_name=self.name,
            status=GateStatus.PASSED,
            passed=True,
            evidence=[
                Evidence("schema_name", state.schema_name),
                Evidence("scheme_id", state.scheme_id),
                Evidence("scheme_version", state.scheme_version),
                Evidence("version_status", state.version_status),
                Evidence("registry_status", state.registry_status),
                Evidence("preflight_table_counts", state.table_counts),
                Evidence("activation_performed", False),
                Evidence("business_writes_performed", False),
                Evidence("audit_path", str(audit_path)),
            ],
            errors=[],
            started_at=started_at,
            finished_at=utc_now(),
            report_path=audit_path,
        )


def _assert_audit_writable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".bootstrap-preflight.", dir=path.parent)
    os.close(fd)
    Path(temporary_name).unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: dict) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _create_engine():
    from scheduler.repository import create_engine_from_env

    return create_engine_from_env()
