from __future__ import annotations

import os
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
        return cls(
            user=os.getenv("BOND_DB_USER", "root"),
            password=os.getenv("BOND_DB_PASSWORD", ""),
            host=os.getenv("BOND_DB_HOST", "localhost"),
            port=int(os.getenv("BOND_DB_PORT", "3306")),
            database=os.getenv("BOND_DB_NAME", "bond_db"),
            charset=os.getenv("BOND_DB_CHARSET", "utf8mb4"),
        )
