from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


SOURCE_DATABASE_CONFIG = {
    "version": "source-runtime-database-v1",
    "user": "source_reader",
    "password": "injected-source-secret",
    "host": "127.0.0.1",
    "port": 43306,
    "database": "bfl_source_test",
    "charset": "utf8mb4",
}


def _write_packaged_config(path: Path) -> None:
    path.write_text(
        "from __future__ import annotations\n"
        "from typing import Any\n"
        "DB_CONFIG: dict[str, Any] = {\n"
        '    "user": "packaged-user",\n'
        '    "password": "packaged-password-must-not-run",\n'
        '    "host": "packaged.invalid",\n'
        '    "port": 3306,\n'
        '    "database": "packaged_database",\n'
        '    "charset": "utf8mb4",\n'
        "}\n",
        encoding="utf-8",
    )


def _write_private_binding(path: Path) -> None:
    path.write_text(
        json.dumps(
            SOURCE_DATABASE_CONFIG,
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


class SourceRunnerDatabaseIsolationTests(unittest.TestCase):
    def test_missing_dedicated_configuration_does_not_fall_back_to_service_database(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            load_source_runtime_database_config,
        )

        legacy_environment = {
            "BOND_DB_USER": "legacy-user",
            "BOND_DB_PASSWORD": "legacy-password",
            "BOND_DB_HOST": "127.0.0.1",
            "BOND_DB_PORT": "3306",
            "BOND_DB_NAME": "bond_db",
        }
        with (
            patch.dict(os.environ, legacy_environment, clear=True),
            self.assertRaisesRegex(
                RuntimeError,
                "source runner database injection is incomplete",
            ),
        ):
            load_source_runtime_database_config()

    def test_source_subprocess_environment_drops_all_database_credentials(
        self,
    ) -> None:
        from shared.source_runtime_database import (
            source_subprocess_environment,
        )

        environment = {
            "BFL_SOURCE_DB_CONFIG_ROOT": "/private",
            "BFL_SOURCE_DB_CONFIG_PATH": "/private/source-db.json",
            "BOND_DB_USER": "service-user",
            "BOND_DB_PASSWORD": "service-secret",
            "BOND_DB_HOST": "production.invalid",
            "BOND_DB_PORT": "3306",
            "BOND_DB_NAME": "bond_db",
            "BOND_DB_CHARSET": "utf8mb4",
            "MYSQL_PWD": "mysql-secret",
            "DATABASE_URL": "mysql://user:password@production/bond_db",
            "KEEP_ME": "safe-value",
        }
        child = source_subprocess_environment(environment)

        self.assertEqual(child["KEEP_ME"], "safe-value")
        self.assertEqual(
            child["PYTHONDONTWRITEBYTECODE"],
            "1",
        )
        for key in environment:
            if (
                key.startswith("BFL_SOURCE_DB_")
                or key.startswith("BOND_DB_")
                or key in {"MYSQL_PWD", "DATABASE_URL"}
            ):
                self.assertNotIn(key, child)


def test_prepare_private_source_runtime_tree_adds_owner_write(
    tmp_path: Path,
) -> None:
    """私有 source 副本保留原只读内容，但允许源算法覆写默认参数。"""
    from shared.source_runtime_database import (
        prepare_private_source_runtime_tree,
    )

    params_dir = (
        tmp_path / "monthly_project" / "hyper_params" / "2026-06-19"
    )
    params_dir.mkdir(parents=True)
    params = params_dir / "params.json"
    params.write_text("{}")
    params_dir.chmod(0o500)
    params.chmod(0o400)

    prepare_private_source_runtime_tree(tmp_path)

    assert params.stat().st_mode & stat.S_IWUSR
    params.write_text('{"ok": true}')


_SOURCE_FAMILIES = {
    "daily": ("daily_0629", "bfl_daily0629_source_", "daily 0629", "DAILY_0629_SOURCE_PYTHON", "D1Y"),
    "monthly": ("monthly", "bfl_monthly_source_", "monthly", "MONTHLY_SOURCE_PYTHON", "M1Y"),
    "weekly": ("weekly_average_lgbm", "bfl_weekly_avg_source_", "weekly average", "WEEKLY_AVERAGE_SOURCE_PYTHON", "W1Y"),
}


@pytest.fixture(params=tuple(_SOURCE_FAMILIES))
def source_runner(request, tmp_path, monkeypatch):
    """由真实三类运行入口验证冻结包与私有副本，只替换算法进程和系统 quarantine。"""
    from shared import source_runtime_database

    kind = request.param
    module_name, prefix, label, interpreter_env, frequency = _SOURCE_FAMILIES[kind]
    runner = import_module(f"shared.{module_name}_source_runner")
    source_root = tmp_path / "source"
    source_root.mkdir()
    original_config = source_root / "db_config.py"
    _write_packaged_config(original_config)
    original_config.chmod(0o400)
    original_bytes = original_config.read_bytes()
    (source_root / "runner.bin").write_bytes(b"frozen-source")
    pycache = source_root / "__pycache__"
    pycache.mkdir()
    (pycache / "db_config.cpython-312.pyc").write_bytes(b"unchecked-bytecode")
    for directory in ("daily_project/src", "monthly_project/src", "weekly_project/src"):
        (source_root / directory).mkdir(parents=True)
    binding = tmp_path / "source-db.json"
    _write_private_binding(binding)
    monkeypatch.setenv("BFL_SOURCE_DB_CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("BFL_SOURCE_DB_CONFIG_PATH", str(binding))
    monkeypatch.setenv("DAILY_0629_SOURCE_CACHE_DISABLE", "1")
    monkeypatch.setenv("MONTHLY_SOURCE_CACHE_DISABLE", "1")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    original_hash = runner.source_package_tree_sha256(source_root)
    evidence = SimpleNamespace(
        source_package_path=source_root, source_package_hash=original_hash,
        runner_module="src.pipeline", live_runner_module="src.live",
    )
    roots = []
    commands = []

    def quarantine(root):
        roots.append(root)
        # 在系统 quarantine 操作边界上，副本已隔离且可写，尚未注入私有配置。
        assert root != source_root
        assert root.parent.name.startswith(prefix)
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert (root / "db_config.py").read_bytes() == original_bytes
        assert (root / "db_config.py").stat().st_mode & stat.S_IWUSR
        assert not list(root.rglob("*.pyc"))
        assert not list(root.rglob("__pycache__"))

    def algorithm(command, *, cwd, env, timeout):
        root = Path(cwd) if kind == "daily" else Path(cwd).parent
        namespace = {}
        runtime_config = root / "db_config.py"
        exec(runtime_config.read_text(encoding="utf-8"), namespace)
        assert namespace["DB_CONFIG"] == {
            key: value for key, value in SOURCE_DATABASE_CONFIG.items() if key != "version"
        }
        assert stat.S_IMODE(runtime_config.stat().st_mode) == 0o600
        assert original_config.read_bytes() == original_bytes
        commands.append((list(command), dict(env)))
        filename = {
            "daily": "daily_selected_prediction_rows.csv",
            "monthly": "monthly_selected_predictions.csv",
            "weekly": "weekly_prediction_detail.csv",
        }[kind]
        output = root / f"{kind}_project/output/2026-11-08/prediction" / filename
        output.parent.mkdir(parents=True)
        output.write_text(f"frequency,pred_label\n{frequency},1\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(source_runtime_database, "clear_macos_quarantine", quarantine)
    monkeypatch.setattr(runner, "run_source_subprocess", algorithm)
    run = getattr(runner, f"run_source_{kind}_live")
    return SimpleNamespace(
        kind=kind, runner=runner, source_root=source_root, original_config=original_config,
        original_bytes=original_bytes, original_hash=original_hash, scratch=scratch,
        prefix=prefix, label=label, interpreter_env=interpreter_env, frequency=frequency,
        evidence=evidence, roots=roots, commands=commands,
        quarantine=quarantine,
        run=lambda: run(evidence, predict_date="2026-11-08"),
    )


def _assert_private_runtime_removed(source):
    assert list(source.scratch.iterdir()) == []
    assert all(not root.exists() for root in source.roots)


def test_source_runner_preserves_original_and_injects_only_private_copy(source_runner):
    rows = source_runner.run()
    assert rows[0]["frequency"] == source_runner.frequency
    assert source_runner.original_config.read_bytes() == source_runner.original_bytes
    assert stat.S_IMODE(source_runner.original_config.stat().st_mode) == 0o400
    assert source_runner.runner.source_package_tree_sha256(source_runner.source_root) == source_runner.original_hash
    _assert_private_runtime_removed(source_runner)


@pytest.mark.parametrize("phase", ["setup", "execution"])
def test_source_runner_cleans_private_copy_on_base_exception(source_runner, monkeypatch, phase):
    from shared import source_runtime_database

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("interrupted")

    if phase == "setup":
        monkeypatch.setattr(source_runtime_database, "clear_macos_quarantine", interrupt)
    else:
        monkeypatch.setattr(source_runner.runner, "run_source_subprocess", interrupt)
    with pytest.raises(KeyboardInterrupt, match="interrupted"):
        source_runner.run()
    assert source_runner.original_config.read_bytes() == source_runner.original_bytes
    _assert_private_runtime_removed(source_runner)


def test_source_runner_refuses_changed_private_copy_before_injection(source_runner, monkeypatch):
    from shared import source_runtime_database

    def mutate_copy(root):
        source_runner.quarantine(root)
        (root / "runner.bin").write_bytes(b"corrupted-copy")

    monkeypatch.setattr(source_runtime_database, "clear_macos_quarantine", mutate_copy)
    with pytest.raises(RuntimeError) as error:
        source_runner.run()
    assert str(error.value) == (
        f"{source_runner.label} copied source package hash differs from frozen source identity"
    )
    assert source_runner.original_config.read_bytes() == source_runner.original_bytes
    _assert_private_runtime_removed(source_runner)


def test_source_runner_cleans_copy_when_private_database_seam_is_missing(source_runner, monkeypatch):
    from shared import source_runtime_database

    source_runner.original_config.unlink()
    source_runner.evidence.source_package_hash = source_runner.runner.source_package_tree_sha256(source_runner.source_root)
    monkeypatch.setattr(source_runtime_database, "clear_macos_quarantine", lambda root: None)
    with pytest.raises(RuntimeError, match="source package has no runtime db_config.py seam"):
        source_runner.run()
    _assert_private_runtime_removed(source_runner)


@pytest.mark.parametrize("selection", ["explicit", "local", "conda"])
def test_source_runner_preserves_interpreter_selection(source_runner, monkeypatch, tmp_path, selection):
    home = tmp_path / "service-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for _, _, _, environment_name, _ in _SOURCE_FAMILIES.values():
        monkeypatch.delenv(environment_name, raising=False)
    if selection == "explicit":
        for _, _, _, environment_name, _ in _SOURCE_FAMILIES.values():
            monkeypatch.setenv(environment_name, "/other-family/python")
        monkeypatch.setenv(source_runner.interpreter_env, "/selected/python")
        expected = ["/selected/python"]
    elif selection == "local":
        local = home / "miniconda3/envs/forecast_env/bin/python"
        local.parent.mkdir(parents=True)
        local.touch()
        expected = [str(local)]
    else:
        expected = ["conda", "run", "--no-capture-output", "-n", "forecast_env", "python"]
    source_runner.run()
    command, environment = source_runner.commands[0]
    if source_runner.kind == "daily":
        assert command == ["bash", "daily_project/src/run_daily.sh", "run", "2026-11-08"]
        assert environment["PYTHON_BIN"] == expected[0]
    else:
        assert command[:len(expected)] == expected
        assert command[len(expected)] == ("-c" if source_runner.kind == "monthly" else "-m")
    _assert_private_runtime_removed(source_runner)
