from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
        "BFL_DATABASE_ENV_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


def _values(**overrides: str) -> dict[str, str]:
    values = {
        "BOND_DB_USER": "runtime_user",
        "BOND_DB_PASSWORD": "secret",
        "BOND_DB_HOST": "127.0.0.1",
        "BOND_DB_PORT": "3307",
        "BOND_DB_NAME": "bond_candidate",
        "BOND_DB_CHARSET": "utf8mb4",
    }
    values.update(overrides)
    return values


def _write_private_environment(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_import_does_not_load_project_dotenv_or_modify_environment(
    tmp_path: Path,
) -> None:
    source = Path(__file__).resolve().parents[1] / "shared" / "db_config.py"
    package = tmp_path / "shared"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(source, package / "db_config.py")
    (tmp_path / ".env").write_text(
        "BOND_DB_USER=must_not_be_loaded\n",
        encoding="utf-8",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"BOND_DB_USER", "PYTHONPATH"}
    }
    environment["PYTHONPATH"] = str(tmp_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; import shared.db_config; "
                "print('unchanged' if 'BOND_DB_USER' not in os.environ else 'changed')"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "unchanged"


def test_database_configs_are_pure_and_independent() -> None:
    from shared.db_config import DatabaseConfig

    first = DatabaseConfig.from_mapping(_values(BOND_DB_NAME="first"))
    second = DatabaseConfig.from_mapping(_values(BOND_DB_NAME="second"))

    assert first.database == "first"
    assert second.database == "second"
    assert first != second


def test_scheduler_engine_accepts_explicit_config_without_reading_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scheduler import repository
    from shared.db_config import DatabaseConfig

    config = DatabaseConfig.from_mapping(_values())
    monkeypatch.setattr(
        DatabaseConfig,
        "from_env",
        classmethod(
            lambda cls: (_ for _ in ()).throw(
                AssertionError("environment must not be read")
            )
        ),
    )

    engine = repository.create_engine_from_env(config)
    try:
        assert engine.url.username == "runtime_user"
        assert engine.url.host == "127.0.0.1"
        assert engine.url.port == 3307
        assert engine.url.database == "bond_candidate"
    finally:
        engine.dispose()


def test_database_config_loads_explicit_private_environment_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.db_config import DatabaseConfig

    env_path = tmp_path / "bond-factor-lab.env"
    _write_private_environment(
        env_path,
        {
            **_values(BOND_DB_PASSWORD="'secret with spaces'"),
            "IGNORED_RUNTIME_SETTING": "must-not-load",
        },
    )
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


def test_explicit_file_allows_same_environment_but_rejects_conflicts(
    tmp_path: Path,
) -> None:
    from shared.db_config import DatabaseConfig

    env_path = tmp_path / "bond-factor-lab.env"
    values = _values()
    _write_private_environment(env_path, values)
    same = {**values, "BFL_DATABASE_ENV_FILE": str(env_path)}

    assert DatabaseConfig.from_env(same).host == "127.0.0.1"
    with pytest.raises(RuntimeError, match="conflicts with explicit file"):
        DatabaseConfig.from_env({**same, "BOND_DB_HOST": "other-host"})

    overridden = DatabaseConfig.from_env(
        {**same, "BOND_DB_HOST": "other-host"},
        allow_file_environment_override=True,
    )
    assert overridden.host == "other-host"


def test_explicit_file_matches_service_environment_literal_value_grammar(
    tmp_path: Path,
) -> None:
    from shared.db_config import DatabaseConfig

    env_path = tmp_path / "bond-factor-lab.env"
    values = _values(BOND_DB_PASSWORD=r"literal\password with spaces")
    _write_private_environment(env_path, values)

    config = DatabaseConfig.from_env(
        {**values, "BFL_DATABASE_ENV_FILE": str(env_path)}
    )

    assert config.password == r"literal\password with spaces"


def test_explicit_file_does_not_fill_missing_values_from_environment(
    tmp_path: Path,
) -> None:
    from shared.db_config import DatabaseConfig

    env_path = tmp_path / "bond-factor-lab.env"
    file_values = _values()
    file_values.pop("BOND_DB_HOST")
    _write_private_environment(env_path, file_values)

    with pytest.raises(RuntimeError, match="conflicts with explicit file"):
        DatabaseConfig.from_env(
            {**_values(), "BFL_DATABASE_ENV_FILE": str(env_path)}
        )


@pytest.mark.parametrize(
    "missing",
    ("BOND_DB_USER", "BOND_DB_PASSWORD", "BOND_DB_HOST", "BOND_DB_NAME"),
)
def test_database_config_rejects_missing_required_values(missing: str) -> None:
    from shared.db_config import DatabaseConfig

    values = _values()
    values.pop(missing)
    with pytest.raises(ValueError, match="missing required keys"):
        DatabaseConfig.from_mapping(values)


@pytest.mark.parametrize("port", ("invalid", "0", "65536"))
def test_database_config_rejects_invalid_port(port: str) -> None:
    from shared.db_config import DatabaseConfig

    with pytest.raises(ValueError, match="BOND_DB_PORT"):
        DatabaseConfig.from_mapping(_values(BOND_DB_PORT=port))


def test_empty_password_requires_explicit_opt_in() -> None:
    from shared.db_config import DatabaseConfig

    values = _values(BOND_DB_PASSWORD="")
    with pytest.raises(ValueError, match="BOND_DB_PASSWORD cannot be empty"):
        DatabaseConfig.from_mapping(values)

    assert DatabaseConfig.from_mapping(
        values,
        allow_empty_password=True,
    ).password == ""


def test_database_environment_file_rejects_duplicate_and_oversized_input(
    tmp_path: Path,
) -> None:
    from shared.db_config import DatabaseConfig

    duplicate = tmp_path / "duplicate.env"
    _write_private_environment(
        duplicate,
        {**_values(), "BOND_DB_USER": "first"},
    )
    with duplicate.open("a", encoding="utf-8") as handle:
        handle.write("BOND_DB_USER=second\n")
    with pytest.raises(RuntimeError, match="duplicate keys"):
        DatabaseConfig.from_env(
            {"BFL_DATABASE_ENV_FILE": str(duplicate)}
        )

    oversized = tmp_path / "oversized.env"
    oversized.write_bytes(b"#" * (64 * 1024 + 1))
    oversized.chmod(0o600)
    with pytest.raises(RuntimeError, match="private regular file|too large"):
        DatabaseConfig.from_env(
            {"BFL_DATABASE_ENV_FILE": str(oversized)}
        )


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
