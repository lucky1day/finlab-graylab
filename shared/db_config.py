from __future__ import annotations

import os
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()
else:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


DATABASE_ENV_FILE_ENV = "BFL_DATABASE_ENV_FILE"
_DATABASE_ENV_KEYS = frozenset(
    {
        "BOND_DB_USER",
        "BOND_DB_PASSWORD",
        "BOND_DB_HOST",
        "BOND_DB_PORT",
        "BOND_DB_NAME",
        "BOND_DB_CHARSET",
    }
)
_MAX_DATABASE_ENV_FILE_BYTES = 64 * 1024


def _read_explicit_database_environment() -> dict[str, str]:
    """读取一次性 runner 显式传入的私有数据库环境文件。"""
    configured = os.getenv(DATABASE_ENV_FILE_ENV)
    if configured is None:
        return {}
    path = Path(configured)
    if not path.is_absolute():
        raise RuntimeError(
            "database environment file must be an absolute path"
        )
    try:
        details = path.lstat()
    except OSError as exc:
        raise RuntimeError("database environment file is unavailable") from exc
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) & 0o077
        or details.st_size > _MAX_DATABASE_ENV_FILE_BYTES
    ):
        raise RuntimeError(
            "database environment file must be a private regular file"
        )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError("database environment file is unreadable") from exc

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in _DATABASE_ENV_KEYS:
            continue
        if key in values:
            raise RuntimeError(
                "database environment file contains duplicate keys"
            )
        try:
            tokens = shlex.split(raw_value, comments=False, posix=True)
        except ValueError as exc:
            raise RuntimeError(
                "database environment file contains invalid values"
            ) from exc
        if len(tokens) > 1:
            raise RuntimeError(
                "database environment file contains invalid values"
            )
        values[key] = tokens[0] if tokens else ""
    return values


@dataclass(frozen=True)
class DatabaseConfig:
    """数据库连接配置，从环境变量读取。"""

    user: str
    password: str
    host: str = "localhost"
    port: int = 3306
    database: str = "bond_db"
    charset: str = "utf8mb4"

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        """从 BOND_DB_* 环境变量创建数据库配置。"""
        file_values = _read_explicit_database_environment()

        def value(name: str, default: str) -> str:
            return os.getenv(name, file_values.get(name, default))

        return cls(
            user=value("BOND_DB_USER", "root"),
            password=value("BOND_DB_PASSWORD", ""),
            host=value("BOND_DB_HOST", "localhost"),
            port=int(value("BOND_DB_PORT", "3306")),
            database=value("BOND_DB_NAME", "bond_db"),
            charset=value("BOND_DB_CHARSET", "utf8mb4"),
        )
