"""每次预测的数据质量与结果日志；缺数不阻断，日志不伪造方向。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import sys
import stat
import threading
from typing import Any, Iterator
import uuid
from zoneinfo import ZoneInfo

_RUN_ID: ContextVar[int | None] = ContextVar('prediction_audit_run_id', default=None)
_LOCK = threading.Lock()


@contextmanager
def audit_run_context(run_id: int | None) -> Iterator[None]:
    """自然run在当前线程绑定审计身份，不建立第二份运行状态。"""
    token = _RUN_ID.set(run_id)
    try:
        yield
    finally:
        _RUN_ID.reset(token)


def current_run_id() -> int | None:
    """Native子进程仅从父进程显式传入的本次run_id读取关联。"""
    value = _RUN_ID.get()
    if value is not None:
        return value
    raw = os.environ.get('BFL_PREDICTION_RUN_ID', '')
    return int(raw) if raw.isdigit() else None


def emit_prediction_audit(event: str, *, scheme_id: str, predict_date: str,
                          **details: Any) -> dict[str, Any]:
    """追加JSONL并输出stderr摘要；文件不可写时保留完整stderr证据，不阻止模型。"""
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='milliseconds')
    payload = {'event': event, 'audit_id': uuid.uuid4().hex, 'recorded_at': stamp,
               'scheme_id': scheme_id, 'predict_date': predict_date,
               'run_id': current_run_id(), 'pid': os.getpid(), **details}
    line = json.dumps(payload, ensure_ascii=False, default=str, separators=(',', ':')) + '\n'
    root = str(os.environ.get('BFL_RUNTIME_ROOT') or '').strip()
    base = Path(root) / 'logs' if root else Path(__file__).resolve().parents[1] / 'outputs/runtime/logs'
    relative = f'prediction-input-quality/{datetime.now(ZoneInfo("Asia/Shanghai")).date()}.jsonl'
    try:
        directory = base / 'prediction-input-quality'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise OSError('audit directory is a symlink')
        if not _LOCK.acquire(blocking=False):
            raise BlockingIOError('audit thread lock is busy')
        try:
            descriptor = os.open(base / relative, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            with os.fdopen(descriptor, 'a', encoding='utf-8') as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise OSError('audit sink must be a regular file')
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            _LOCK.release()
        _safe_stderr(json.dumps({'event': event, 'audit_id': payload['audit_id'],
                          'scheme_id': scheme_id, 'predict_date': predict_date,
                          'run_id': payload['run_id'], 'status': details.get('status'),
                          'audit_log': relative}, ensure_ascii=False))
        return {'audit_id': payload['audit_id'], 'audit_log': relative}
    except OSError as exc:
        _safe_stderr(line.rstrip())
        _safe_stderr(json.dumps({'event': 'prediction_audit_file_error', 'audit_id': payload['audit_id'],
                          'scheme_id': scheme_id, 'predict_date': predict_date,
                          'error_type': type(exc).__name__, 'full_evidence': 'stderr'}, ensure_ascii=False),
              )
        return {'audit_id': payload['audit_id'], 'audit_log': 'stderr'}


def relay_native_audit_stderr(stderr: str | bytes | None, *, scheme_id: str,
                              predict_date: str) -> None:
    """只转发本审计协议的JSON行；不泄露算法的未识别stderr或连接信息。"""
    import re

    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', errors='replace')
    for line in (stderr or '').splitlines():
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        if (value.get('event') not in {'prediction_input_quality', 'native_input_audit_source_observation',
                                       'prediction_audit_file_error'}
                or value.get('scheme_id') != scheme_id or value.get('predict_date') != predict_date
                or not re.fullmatch(r'[0-9a-f]{32}', str(value.get('audit_id', '')))):
            continue
        _safe_stderr(line)


def _safe_stderr(message: str) -> None:
    """日志sink自身异常不能改变模型执行结果。"""
    try:
        print(message, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass
