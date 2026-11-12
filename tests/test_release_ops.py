"""release_ops 手工运维入口：环境合成必须绑定目标、拒绝外部 Python 路径并且不冒充自然控制面。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_ops  # noqa: E402
from run_launchd_release import LaunchdReleaseError  # noqa: E402

RELEASE_ENV = {
    "BFL_RELEASE_COMMIT": "a" * 40,
    "BFL_RUNTIME_ROOT": "/var/lib/bond-factor-lab/state",
    "NUMBA_CACHE_DIR": "/var/lib/bond-factor-lab/state/cache/numba",
    "MPLCONFIGDIR": "/var/lib/bond-factor-lab/state/cache/mpl",
}


@pytest.fixture(autouse=True)
def trusted_release_env(monkeypatch):
    monkeypatch.setattr(release_ops, "load_release_environment", lambda root: dict(RELEASE_ENV))


def test_gray_environment_is_bound_and_not_natural_control_plane():
    env = release_ops.build_environment(Path("/r"), "aliyun-gray", {"HOME": "/root", "BOND_FACTOR_LAB_CONTROL_PLANE": "systemd_one_shot"})
    assert env["BFL_DEPLOYMENT_TARGET"] == "aliyun-gray"
    assert env["BFL_DATABASE_ENV_FILE"] == "/etc/bond-factor-lab/bond-factor-lab.env"
    assert env["BFL_RUNTIME_ROOT"] == RELEASE_ENV["BFL_RUNTIME_ROOT"]
    assert "BOND_FACTOR_LAB_CONTROL_PLANE" not in env


@pytest.mark.parametrize("key", ["PYTHONPATH", "PYTHONHOME"])
def test_ambient_python_path_is_rejected(key):
    with pytest.raises(LaunchdReleaseError):
        release_ops.build_environment(Path("/r"), "aliyun-gray", {key: "/elsewhere"})


def test_explicit_target_must_match_host():
    other = "aliyun-gray" if sys.platform == "darwin" else "mac3-production"
    with pytest.raises(LaunchdReleaseError):
        release_ops.detect_target(other)
