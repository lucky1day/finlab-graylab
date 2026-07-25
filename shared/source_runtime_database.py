from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


SOURCE_RUNTIME_DATABASE_CONFIG_PATH_ENV = (
    "BFL_SOURCE_DB_CONFIG_PATH"
)
SOURCE_RUNTIME_DATABASE_CONFIG_ROOT_ENV = (
    "BFL_SOURCE_DB_CONFIG_ROOT"
)
SOURCE_RUNTIME_DATABASE_CONFIG_VERSION = (
    "source-runtime-database-v1"
)
SOURCE_IMMUTABLE_INPUT_TOKEN_ENV = (
    "BFL_SOURCE_IMMUTABLE_INPUT_TOKEN"
)
SOURCE_RUNTIME_SCHEME_IDS = frozenset(
    {
        "daily_1y_xgb_1y13_0629",
        "daily_5y_lgbm_5y10_0629",
        "daily_10y_lgbm_10y04_0629",
        "monthly_1y_rf_top30_0629",
        "monthly_5y_knn_top20_0629",
        "monthly_10y_rf_top5_0629",
        "weekly_avg_1y_lgbm_0529",
        "weekly_avg_5y_lgbm_0529",
        "weekly_avg_10y_lgbm_0529",
    }
)
_CONFIG_KEYS = frozenset(
    {
        "version",
        "user",
        "password",
        "host",
        "port",
        "database",
        "charset",
    }
)
_DATABASE_ENVIRONMENT_NAMES = frozenset(
    {
        SOURCE_RUNTIME_DATABASE_CONFIG_PATH_ENV,
        SOURCE_RUNTIME_DATABASE_CONFIG_ROOT_ENV,
        "BOND_DB_USER",
        "BOND_DB_PASSWORD",
        "BOND_DB_HOST",
        "BOND_DB_PORT",
        "BOND_DB_NAME",
        "BOND_DB_CHARSET",
        "MYSQL_PWD",
        "DATABASE_URL",
        "SQLALCHEMY_DATABASE_URI",
        SOURCE_IMMUTABLE_INPUT_TOKEN_ENV,
    }
)
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_ERROR_OUTPUT_CHARS = 8_000
_SOURCE_PROCESS_TERMINATE_GRACE_SECONDS = 1.0
_SOURCE_PROCESS_ESCAPE_PATTERNS = (
    re.compile(rb"\b(?:os\s*\.\s*)?setsid\s*\("),
    re.compile(rb"\bstart_new_session\s*=\s*True\b"),
    re.compile(rb"\bdaemonize\s*\("),
)
_SOURCE_PROCESS_ESCAPE_SUFFIXES = frozenset(
    {".py", ".sh", ".bash", ".zsh"}
)
_SOURCE_PROCESS_PARENT_WATCHDOG = """\
import os
import signal
import subprocess
import sys
import time

expected_parent_pid = int(sys.argv[1])
child = subprocess.Popen(sys.argv[2:])
while True:
    if os.getppid() != expected_parent_pid:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    returncode = child.poll()
    if returncode is not None:
        if returncode < 0:
            os.kill(os.getpid(), -returncode)
        raise SystemExit(returncode)
    time.sleep(0.05)
"""


@dataclass(frozen=True, repr=False)
class SourceRuntimeDatabaseConfig:
    """归档 source runner 使用的显式、只读数据库绑定。"""

    user: str
    password: str
    host: str
    port: int
    database: str
    charset: str
    config_path: Path

    def __repr__(self) -> str:
        return (
            "SourceRuntimeDatabaseConfig("
            f"user={self.user!r}, password='<redacted>', "
            f"host={self.host!r}, port={self.port!r}, "
            f"database={self.database!r}, charset={self.charset!r}, "
            f"config_path={str(self.config_path)!r})"
        )

    @property
    def cache_identity(self) -> str:
        """返回不包含密码的运行端点身份。"""
        payload = {
            "version": SOURCE_RUNTIME_DATABASE_CONFIG_VERSION,
            "user": self.user,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "charset": self.charset,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


def _validate_source_runtime_database_config_root(
    approved_root: Path,
) -> None:
    """校验 BFL 私有配置根及完整父链，拒绝可替换路径。"""
    try:
        root_stat = approved_root.lstat()
    except OSError as exc:
        raise RuntimeError(
            "source runner database approved private root is unavailable"
        ) from exc
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        raise RuntimeError(
            "source runner database approved private root must be a "
            "regular non-symlink directory"
        )
    if root_stat.st_uid != os.getuid():
        raise RuntimeError(
            "source runner database approved private root must be owned "
            "by the service user"
        )
    if stat.S_IMODE(root_stat.st_mode) != 0o700:
        raise RuntimeError(
            "source runner database approved private root must use "
            "owner-only mode 0700"
        )

    for ancestor in approved_root.parents:
        try:
            ancestor_stat = ancestor.lstat()
        except OSError as exc:
            raise RuntimeError(
                "source runner database approved private root ancestry "
                "is unavailable"
            ) from exc
        if (
            stat.S_ISLNK(ancestor_stat.st_mode)
            or not stat.S_ISDIR(ancestor_stat.st_mode)
        ):
            raise RuntimeError(
                "source runner database approved private root ancestry "
                "must contain only non-symlink directories"
            )
        if ancestor_stat.st_uid not in {0, os.getuid()}:
            raise RuntimeError(
                "source runner database approved private root ancestry "
                "has an unexpected owner"
            )
        mode = stat.S_IMODE(ancestor_stat.st_mode)
        writable_by_others = bool(mode & 0o022)
        trusted_sticky_root = bool(
            ancestor_stat.st_uid == 0
            and mode & stat.S_ISVTX
        )
        if writable_by_others and not trusted_sticky_root:
            raise RuntimeError(
                "source runner database approved private root ancestry "
                "is unsafe"
            )


def load_source_runtime_database_config(
    environ: Mapping[str, str] | None = None,
) -> SourceRuntimeDatabaseConfig:
    """从 owner-only 配置文件加载 source runner 数据库绑定。"""
    environment = os.environ if environ is None else environ
    raw_path = str(
        environment.get(
            SOURCE_RUNTIME_DATABASE_CONFIG_PATH_ENV,
            "",
        )
    ).strip()
    if not raw_path:
        raise RuntimeError(
            "source runner database injection is incomplete: "
            f"missing {SOURCE_RUNTIME_DATABASE_CONFIG_PATH_ENV}"
        )
    path = Path(raw_path)
    if not path.is_absolute():
        raise RuntimeError(
            "source runner database config path must be absolute"
        )
    raw_root = str(
        environment.get(
            SOURCE_RUNTIME_DATABASE_CONFIG_ROOT_ENV,
            "",
        )
    ).strip()
    if not raw_root:
        raise RuntimeError(
            "source runner database injection is incomplete: "
            f"missing {SOURCE_RUNTIME_DATABASE_CONFIG_ROOT_ENV}"
        )
    approved_root = Path(raw_root)
    if not approved_root.is_absolute():
        raise RuntimeError(
            "source runner database approved private root must be absolute"
        )
    _validate_source_runtime_database_config_root(approved_root)
    if path.parent != approved_root:
        raise RuntimeError(
            "source runner database config must stay inside the "
            "approved private root"
        )
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RuntimeError(
            "source runner database config is unavailable"
        ) from exc
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
    ):
        raise RuntimeError(
            "source runner database config must be a regular "
            "non-symlink file"
        )
    if path_stat.st_uid != os.getuid():
        raise RuntimeError(
            "source runner database config must be owned by the "
            "service user"
        )
    if stat.S_IMODE(path_stat.st_mode) != 0o600:
        raise RuntimeError(
            "source runner database config must use owner-only mode 0600"
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(
            "source runner database config could not be opened safely"
        ) from exc
    try:
        opened_stat = os.fstat(descriptor)
        if (
            opened_stat.st_dev != path_stat.st_dev
            or opened_stat.st_ino != path_stat.st_ino
            or not stat.S_ISREG(opened_stat.st_mode)
            or stat.S_IMODE(opened_stat.st_mode) != 0o600
            or opened_stat.st_uid != os.getuid()
        ):
            raise RuntimeError(
                "source runner database config changed while opening"
            )
        raw = os.read(descriptor, _MAX_CONFIG_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_CONFIG_BYTES:
        raise RuntimeError(
            "source runner database config exceeds the size limit"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "source runner database config is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "source runner database config must be a JSON object"
        )
    actual_keys = frozenset(payload)
    if actual_keys != _CONFIG_KEYS:
        raise RuntimeError(
            "source runner database config fields are invalid: "
            f"missing={sorted(_CONFIG_KEYS - actual_keys)}, "
            f"extra={sorted(actual_keys - _CONFIG_KEYS)}"
        )
    if payload["version"] != SOURCE_RUNTIME_DATABASE_CONFIG_VERSION:
        raise RuntimeError(
            "source runner database config version is unsupported"
        )

    text_fields: dict[str, str] = {}
    for field in (
        "user",
        "password",
        "host",
        "database",
        "charset",
    ):
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(
                "source runner database config has an empty or "
                f"invalid {field}"
            )
        text_fields[field] = value.strip() if field != "password" else value
    if text_fields["user"].casefold() == "root":
        raise RuntimeError(
            "source runner database config forbids the root user"
        )
    port = payload["port"]
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
    ):
        raise RuntimeError(
            "source runner database config has an invalid port"
        )
    return SourceRuntimeDatabaseConfig(
        user=text_fields["user"],
        password=text_fields["password"],
        host=text_fields["host"],
        port=port,
        database=text_fields["database"],
        charset=text_fields["charset"],
        config_path=path,
    )


def install_source_runtime_database_config(
    source_root: Path,
    config: SourceRuntimeDatabaseConfig,
) -> None:
    """只覆盖私有运行副本中的 db_config.py。"""
    target = source_root / "db_config.py"
    try:
        target_stat = target.lstat()
    except OSError as exc:
        raise RuntimeError(
            "source package has no runtime db_config.py seam"
        ) from exc
    if (
        stat.S_ISLNK(target_stat.st_mode)
        or not stat.S_ISREG(target_stat.st_mode)
    ):
        raise RuntimeError(
            "source package db_config.py must be a regular "
            "non-symlink file"
        )
    payload = {
        "user": config.user,
        "password": config.password,
        "host": config.host,
        "port": config.port,
        "database": config.database,
        "charset": config.charset,
    }
    rendered = (
        "from __future__ import annotations\n"
        "from typing import Any\n\n"
        "DB_CONFIG: dict[str, Any] = "
        + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=4,
        )
        + "\n"
    ).encode("utf-8")
    temporary = target.with_name(".db_config.py.bfl-tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, target)
        target.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def assert_source_package_tree_safe(path: Path) -> None:
    """拒绝 source package 中的链接与特殊文件。"""
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError(
            "source package root must be a regular directory"
        )
    for child in path.rglob("*"):
        child_stat = child.lstat()
        if stat.S_ISLNK(child_stat.st_mode):
            raise RuntimeError(
                "source package must not contain symlinks"
            )
        if not (
            stat.S_ISDIR(child_stat.st_mode)
            or stat.S_ISREG(child_stat.st_mode)
        ):
            raise RuntimeError(
                "source package must contain only regular files "
                "and directories"
            )
        if (
            stat.S_ISREG(child_stat.st_mode)
            and child.suffix.casefold()
            in _SOURCE_PROCESS_ESCAPE_SUFFIXES
        ):
            payload = child.read_bytes()
            if any(
                pattern.search(payload)
                for pattern in _SOURCE_PROCESS_ESCAPE_PATTERNS
            ):
                raise RuntimeError(
                    "source package process isolation forbids "
                    "session escape or daemon primitives"
                )


def prepare_private_source_runtime_tree(path: Path) -> None:
    """从私有副本移除所有未冻结 Python 字节码。"""
    assert_source_package_tree_safe(path)
    for child in sorted(
        path.rglob("*.pyc"),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        child.unlink()
    for child in sorted(
        (
            item
            for item in path.rglob("__pycache__")
            if item.is_dir()
        ),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        shutil.rmtree(child)
    if any(path.rglob("*.pyc")) or any(
        item.name == "__pycache__"
        for item in path.rglob("*")
    ):
        raise RuntimeError(
            "private source runtime still contains Python bytecode"
        )


def assert_source_package_identity(
    source_package_path: Path,
    expected_sha256: str,
    *,
    tree_sha256: Callable[[Path], str],
    label: str,
) -> None:
    """在读取任何缓存前重新验证归档 source package 身份。"""
    assert_source_package_tree_safe(source_package_path)
    actual_sha256 = tree_sha256(source_package_path)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            f"{label} source package hash differs from frozen "
            "source identity"
        )


def require_manifest_source_package_sha256(
    manifest: Mapping[str, Any],
    source_package_path: Path,
    *,
    tree_sha256: Callable[[Path], str],
    label: str,
) -> str:
    """读取 manifest 固定摘要并核对归档 package。"""
    expected = str(
        manifest.get("source_package_sha256") or ""
    ).strip()
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise RuntimeError(
            f"{label} manifest source_package_sha256 is missing "
            "or invalid"
        )
    assert_source_package_tree_safe(source_package_path)
    actual = tree_sha256(source_package_path)
    if actual != expected:
        raise RuntimeError(
            f"{label} manifest source_package_sha256 differs "
            "from source package bytes"
        )
    return expected


def source_immutable_input_token(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """读取显式不可变输入 token；缺失时禁止 source cache。"""
    environment = os.environ if environ is None else environ
    raw = environment.get(SOURCE_IMMUTABLE_INPUT_TOKEN_ENV)
    if raw is None or not str(raw).strip():
        return None
    token = str(raw).strip()
    if re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise RuntimeError(
            "source immutable input token must be a lowercase sha256"
        )
    return token


def source_timeout_seconds(
    environment_name: str,
    *,
    default: int = 1800,
    maximum: int = 3600,
) -> int:
    """解析 source runner 有界硬超时。"""
    raw = os.environ.get(environment_name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{environment_name} must be an integer"
        ) from exc
    if not 1 <= value <= maximum:
        raise RuntimeError(
            f"{environment_name} must be between 1 and {maximum}"
        )
    return value


def write_private_json_atomic(
    path: Path,
    payload: Any,
) -> None:
    """以 0600 唯一临时文件原子发布 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(raw_temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                sort_keys=True,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def frozen_source_runtime_database_config(
    config: SourceRuntimeDatabaseConfig,
) -> Iterator[Path]:
    """为单次 attempt 创建不可替换的私有数据库绑定副本。"""
    with tempfile.TemporaryDirectory(
        prefix="bfl-source-db-binding-",
    ) as tmpdir:
        root = Path(tmpdir).resolve()
        root.chmod(0o700)
        path = root / "source-db.json"
        write_private_json_atomic(
            path,
            {
                "version": SOURCE_RUNTIME_DATABASE_CONFIG_VERSION,
                "user": config.user,
                "password": config.password,
                "host": config.host,
                "port": config.port,
                "database": config.database,
                "charset": config.charset,
            },
        )
        yield path


def source_subprocess_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """构建不含任何数据库凭据或配置路径的子进程环境。"""
    source = os.environ if environment is None else environment
    sanitized = {
        name: value
        for name, value in source.items()
        if name not in _DATABASE_ENVIRONMENT_NAMES
        and not name.startswith("BFL_SOURCE_DB_")
        and not name.startswith("BOND_DB_")
    }
    sanitized["PYTHONDONTWRITEBYTECODE"] = "1"
    return sanitized


def run_source_subprocess(
    command: list[str],
    *,
    cwd: Path | str,
    env: Mapping[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """在独立进程组运行 source 命令，并在退出前收口全部后代。"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _SOURCE_PROCESS_PARENT_WATCHDOG,
            str(os.getpid()),
            *command,
        ],
        cwd=str(cwd),
        env=dict(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timeout_error: subprocess.TimeoutExpired | None = None
    stdout: str | bytes | None = None
    stderr: str | bytes | None = None
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timeout_error = exc
    finally:
        _terminate_source_process_group(process)

    if timeout_error is not None:
        drain_error: subprocess.TimeoutExpired | None = None
        try:
            final_stdout, final_stderr = process.communicate(
                timeout=_SOURCE_PROCESS_TERMINATE_GRACE_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            drain_error = exc
            _signal_source_process_group(
                process.pid,
                signal.SIGKILL,
            )
            _close_source_process_pipes(process)
            try:
                process.wait(
                    timeout=(
                        _SOURCE_PROCESS_TERMINATE_GRACE_SECONDS
                    )
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    "source process output cleanup timed out"
                ) from exc
            final_stdout = None
            final_stderr = None
        stdout = (
            final_stdout
            if final_stdout not in (None, "")
            else (
                drain_error.output
                if (
                    drain_error is not None
                    and drain_error.output is not None
                )
                else timeout_error.output
            )
        )
        stderr = (
            final_stderr
            if final_stderr not in (None, "")
            else (
                drain_error.stderr
                if (
                    drain_error is not None
                    and drain_error.stderr is not None
                )
                else timeout_error.stderr
            )
        )
        raise subprocess.TimeoutExpired(
            cmd=command,
            timeout=timeout,
            output=stdout,
            stderr=stderr,
        )

    return subprocess.CompletedProcess(
        args=command,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _terminate_source_process_group(
    process: subprocess.Popen[str],
) -> None:
    process_group_id = process.pid
    if not _source_process_group_exists(process_group_id):
        return
    _signal_source_process_group(
        process_group_id,
        signal.SIGTERM,
    )
    deadline = (
        time.monotonic()
        + _SOURCE_PROCESS_TERMINATE_GRACE_SECONDS
    )
    while (
        time.monotonic() < deadline
        and _source_process_group_exists(process_group_id)
    ):
        if process.poll() is None:
            try:
                process.wait(timeout=0.02)
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(0.02)
    if _source_process_group_exists(process_group_id):
        _signal_source_process_group(
            process_group_id,
            signal.SIGKILL,
        )
    deadline = (
        time.monotonic()
        + _SOURCE_PROCESS_TERMINATE_GRACE_SECONDS
    )
    while (
        time.monotonic() < deadline
        and _source_process_group_exists(process_group_id)
    ):
        if process.poll() is None:
            try:
                process.wait(timeout=0.02)
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(0.02)
    if _source_process_group_exists(process_group_id):
        raise RuntimeError(
            "source process group cleanup could not be confirmed"
        )


def _close_source_process_pipes(
    process: subprocess.Popen[str],
) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _source_process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_source_process_group(
    process_group_id: int,
    signal_number: int,
) -> None:
    try:
        os.killpg(process_group_id, signal_number)
    except ProcessLookupError:
        return


def redact_source_runtime_text(
    value: object,
    config: SourceRuntimeDatabaseConfig,
) -> str:
    """清理 source runner 错误文本中的数据库秘密。"""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    secrets = {
        config.password,
        os.environ.get("BOND_DB_PASSWORD", ""),
        os.environ.get("MYSQL_PWD", ""),
        os.environ.get("DATABASE_URL", ""),
        os.environ.get("SQLALCHEMY_DATABASE_URI", ""),
    }
    for secret in sorted(
        (item for item in secrets if item),
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "<redacted>")
    if len(text) > _MAX_ERROR_OUTPUT_CHARS:
        text = text[-_MAX_ERROR_OUTPUT_CHARS:]
    return text


def assert_source_runtime_payload_safe(
    value: Any,
    config: SourceRuntimeDatabaseConfig,
) -> None:
    """拒绝把数据库密码带入结果或缓存。"""
    if config.password and _value_contains_secret(
        value,
        config.password,
    ):
        raise RuntimeError(
            "source runner output contains database credentials"
        )


def _value_contains_secret(value: Any, secret: str) -> bool:
    if isinstance(value, str):
        return secret in value
    if isinstance(value, bytes):
        return secret.encode("utf-8") in value
    if isinstance(value, Mapping):
        return any(
            _value_contains_secret(key, secret)
            or _value_contains_secret(item, secret)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(
            _value_contains_secret(item, secret)
            for item in value
        )
    return False
