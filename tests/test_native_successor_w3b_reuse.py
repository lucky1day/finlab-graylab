"""原件复用的执行环境安全边界；自包含 fixture，不依赖单次 ECS 产物。"""

import hashlib
from types import SimpleNamespace

import pytest

from harness import native_successor_migration as migration


def test_runtime_verification_rejects_mac_before_capture(monkeypatch, tmp_path):
    monkeypatch.setattr(migration.sys, "platform", "darwin")
    with pytest.raises(ValueError, match="captured ECS Native runtime"):
        migration._verify_reviewed_w3b_runtime({}, tmp_path)


def test_runtime_inventory_and_bytes_are_rechecked(monkeypatch, tmp_path):
    from scheduler import blackbox_v2_runner as runner

    monkeypatch.setattr(migration.sys, "platform", "linux")
    monkeypatch.setenv("BOND_ALGO_CONDA_ENV", "forecast_env")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    for key in ("LC_ALL", "TZ"):
        monkeypatch.delenv(key, raising=False)
    binding = {"installed_locale": {"LANG": "en_US.UTF-8"}, "environment": {}}
    runtimes = {}
    for kind, name in (("native", "forecast_env"), ("successor", "forecast_env_blackbox_v1")):
        prefix = tmp_path / name
        (prefix / "conda-meta").mkdir(parents=True)
        executable = prefix / "python"
        executable.write_bytes(b"captured interpreter")
        record = prefix / "conda-meta/python.json"
        record.write_bytes(b"captured package record")
        runtimes[name] = SimpleNamespace(executable=executable, prefix=prefix)
        binding["environment"][kind] = {
            "executable": str(executable), "prefix": str(prefix),
            "files": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (executable, record)},
            "effective_locale": {"LANG": "en_US.UTF-8", **({"TZ": "Asia/Shanghai"} if kind == "successor" else {})},
        }
    monkeypatch.setattr(runner, "_python_runtime", lambda profile: runtimes[profile.conda_env])
    migration._verify_reviewed_w3b_runtime(binding, tmp_path)
    record.write_bytes(b"changed package record")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        migration._verify_reviewed_w3b_runtime(binding, tmp_path)
    record.write_bytes(b"captured package record")
    (record.parent / "added.json").write_bytes(b"additional package")
    with pytest.raises(ValueError, match="inventory changed"):
        migration._verify_reviewed_w3b_runtime(binding, tmp_path)
