from __future__ import annotations

from pathlib import Path
import sys

from sqlalchemy import text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.repository import create_engine_from_env


def main() -> None:
    """按文件名顺序执行 migrations/*.sql。"""
    engine = create_engine_from_env()
    try:
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            statements = [part.strip() for part in sql.split(";") if part.strip()]
            with engine.begin() as conn:
                for statement in statements:
                    conn.execute(text(statement))
            print(f"applied {path.name}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
