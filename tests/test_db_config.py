from __future__ import annotations

import os
from pathlib import Path

import pytest


def _clear_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BOND_DB_USER",
        "BOND_DB_PASSWORD",
        "BOND_DB_HOST",
        "BOND_DB_PORT",
        "BOND_DB_NAME",
        "BOND_DB_CHARSET",
    ):
        monkeypatch.delenv(name, raising=False)


def test_database_config_loads_explicit_private_environment_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.db_config import DatabaseConfig

    env_path = tmp_path / "bond-factor-lab.env"
    env_path.write_text(
        "\n".join(
            (
                "BOND_DB_USER=runtime_user",
                "BOND_DB_PASSWORD='secret with spaces'",
                "BOND_DB_HOST=127.0.0.1",
                "BOND_DB_PORT=3307",
                "BOND_DB_NAME=bond_candidate",
                "BOND_DB_CHARSET=utf8mb4",
                "IGNORED_RUNTIME_SETTING=must-not-load",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    _clear_database_environment(monkeypatch)
    monkeypatch.delenv("IGNORED_RUNTIME_SETTING", raising=False)
    monkeypatch.setenv("BFL_DATABASE_ENV_FILE", str(env_path))

    config = DatabaseConfig.from_env()

    assert config.user == "runtime_user"
    assert config.password == "secret with spaces"
    assert config.host == "127.0.0.1"
    assert config.port == 3307
    assert config.database == "bond_candidate"
    assert config.charset == "utf8mb4"
    assert "IGNORED_RUNTIME_SETTING" not in os.environ


def test_database_config_rejects_non_private_environment_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.db_config import DatabaseConfig

    env_path = tmp_path / "bond-factor-lab.env"
    env_path.write_text("BOND_DB_USER=runtime_user\n", encoding="utf-8")
    env_path.chmod(0o644)
    _clear_database_environment(monkeypatch)
    monkeypatch.setenv("BFL_DATABASE_ENV_FILE", str(env_path))

    with pytest.raises(RuntimeError, match="private regular file"):
        DatabaseConfig.from_env()
