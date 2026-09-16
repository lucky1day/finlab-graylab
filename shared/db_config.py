from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


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
_REQUIRED_DATABASE_ENV_KEYS = (
    "BOND_DB_USER",
    "BOND_DB_PASSWORD",
    "BOND_DB_HOST",
    "BOND_DB_NAME",
)


def _read_explicit_database_environment(
    configured: str | os.PathLike[str],
) -> dict[str, str]:
    """读取一次性 runner 显式传入的私有数据库环境文件。"""
    path = Path(configured)
    if not path.is_absolute():
        raise RuntimeError(
            "database environment file must be an absolute path"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("database environment file is unavailable") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) & 0o077
            or details.st_size > _MAX_DATABASE_ENV_FILE_BYTES
        ):
            raise RuntimeError(
                "database environment file must be a private regular file"
            )
        chunks: list[bytes] = []
        remaining = _MAX_DATABASE_ENV_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > _MAX_DATABASE_ENV_FILE_BYTES:
            raise RuntimeError("database environment file is too large")
        try:
            lines = payload.decode("utf-8").splitlines()
        except UnicodeError as exc:
            raise RuntimeError(
                "database environment file is not valid UTF-8"
            ) from exc
    except OSError as exc:
        raise RuntimeError("database environment file is unreadable") from exc
    finally:
        os.close(descriptor)

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(
                "database environment file contains invalid lines"
            )
        key, raw_value = line.split("=", 1)
        if key != key.strip():
            raise RuntimeError(
                "database environment file contains invalid lines"
            )
        if key not in _DATABASE_ENV_KEYS:
            continue
        if key in values:
            raise RuntimeError(
                "database environment file contains duplicate keys"
            )
        value = raw_value.strip()
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise RuntimeError(
                    "database environment file contains invalid values"
                )
            value = value[1:-1]
        elif value[-1:] in {"'", '"'}:
            raise RuntimeError(
                "database environment file contains invalid values"
            )
        if "\x00" in value:
            raise RuntimeError(
                "database environment file contains invalid values"
            )
        values[key] = value
    return values


@dataclass(frozen=True)
class DatabaseConfig:
    """显式数据库连接配置。"""

    user: str
    password: str
    host: str
    database: str
    port: int = 3306
    charset: str = "utf8mb4"

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str],
        *,
        allow_empty_password: bool = False,
    ) -> "DatabaseConfig":
        """从调用方映射纯解析数据库配置，不读取外部状态。"""
        missing = [key for key in _REQUIRED_DATABASE_ENV_KEYS if key not in values]
        if missing:
            raise ValueError(
                "database configuration is missing required keys: "
                + ", ".join(missing)
            )

        def required_text(name: str) -> str:
            value = str(values[name]).strip()
            if not value:
                raise ValueError(f"database configuration {name} cannot be empty")
            return value

        password = str(values["BOND_DB_PASSWORD"])
        if not password and not allow_empty_password:
            raise ValueError(
                "database configuration BOND_DB_PASSWORD cannot be empty"
            )
        raw_port = str(values.get("BOND_DB_PORT", "3306")).strip()
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ValueError(
                "database configuration BOND_DB_PORT must be an integer"
            ) from exc
        if not 1 <= port <= 65_535:
            raise ValueError(
                "database configuration BOND_DB_PORT must be between 1 and 65535"
            )
        charset = str(values.get("BOND_DB_CHARSET", "utf8mb4")).strip()
        if not charset:
            raise ValueError(
                "database configuration BOND_DB_CHARSET cannot be empty"
            )
        return cls(
            user=required_text("BOND_DB_USER"),
            password=password,
            host=required_text("BOND_DB_HOST"),
            database=required_text("BOND_DB_NAME"),
            port=port,
            charset=charset,
        )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        environment_file: str | os.PathLike[str] | None = None,
        allow_file_environment_override: bool = False,
        allow_empty_password: bool = False,
    ) -> "DatabaseConfig":
        """从显式文件或当前环境收集配置后调用纯解析器。"""
        environment = dict(os.environ if environ is None else environ)
        configured_file = (
            environment_file
            if environment_file is not None
            else environment.get(DATABASE_ENV_FILE_ENV)
        )
        ambient = {
            key: environment[key]
            for key in _DATABASE_ENV_KEYS
            if key in environment
        }
        if configured_file is None:
            selected = ambient
        else:
            file_values = _read_explicit_database_environment(configured_file)
            conflicts = sorted(
                key
                for key, value in ambient.items()
                if file_values.get(key) != value
            )
            if conflicts and not allow_file_environment_override:
                raise RuntimeError(
                    "database environment conflicts with explicit file: "
                    + ", ".join(conflicts)
                )
            selected = dict(file_values)
            if allow_file_environment_override:
                selected.update(ambient)
        return cls.from_mapping(
            selected,
            allow_empty_password=allow_empty_password,
        )
