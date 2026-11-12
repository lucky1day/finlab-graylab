"""候选/当前 release 的手工运维入口：绑定本机目标环境后运行 Harness 或只读探针。

在目标机的 `releases/<commit>` 目录内运行，例如::

    python -B scripts/release_ops.py harness -- gate backtest --scheme-id X --predict-date D --persist
    python -B scripts/release_ops.py probe refresh-date
    python -B scripts/release_ops.py probe facts --scheme-id X --since 2026-11-10
    python -B scripts/release_ops.py probe dashboard-digest --out /tmp/dash.json

环境来源与部署手册一致：release 的 `.bfl-release.env`（提交、runtime、cache）、本机服务解释器与
conda、固定的数据库配置文件路径。不读取或打印数据库凭据；不设置自然控制面标记，手工运行不会被当作自然触发。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_launchd_release import (  # noqa: E402
    LaunchdReleaseError,
    load_release_environment,
    load_service_environment,
)

TARGETS = {
    "mac3-production": {
        "python": "/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python",
        "path": "/Users/macstudio0/miniconda3/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "conda": "/Users/macstudio0/miniconda3/bin/conda",
    },
    "aliyun-gray": {
        "python": "/opt/miniconda3/envs/bond_factor_lab_service/bin/python",
        "path": "/opt/miniconda3/condabin:/opt/miniconda3/bin:/usr/local/bin:/usr/bin:/bin",
        "conda": "/opt/miniconda3/bin/conda",
        "database_env_file": "/etc/bond-factor-lab/bond-factor-lab.env",
    },
}
_PROBES = {"refresh-date", "facts", "dashboard-digest"}


def detect_target(explicit: str | None) -> str:
    """按本机平台推断部署目标；显式给出时必须与平台一致。"""
    inferred = "mac3-production" if sys.platform == "darwin" else "aliyun-gray"
    if explicit and explicit != inferred:
        raise LaunchdReleaseError(f"--target {explicit} does not match this host ({inferred})")
    return inferred


def build_environment(release_root: Path, target: str, ambient: dict[str, str]) -> dict[str, str]:
    """合成本次子进程环境；ambient 中的 Python 路径变量与可信值冲突时拒绝。"""
    for key in ("PYTHONHOME", "PYTHONPATH"):
        if key in ambient:
            raise LaunchdReleaseError(f"ambient {key} is forbidden")
    profile = TARGETS[target]
    release_env = load_release_environment(release_root)
    if target == "mac3-production":
        service_env = load_service_environment(release_env["BFL_RUNTIME_ROOT"])
        database_env_file = str(Path(release_env["BFL_RUNTIME_ROOT"]) / "config" / "service.env")
    else:
        service_env = {}
        database_env_file = profile["database_env_file"]
    environment = {key: ambient[key] for key in ("HOME", "USER", "LANG", "TMPDIR") if key in ambient}
    environment.update(service_env)
    environment.update(release_env)
    environment.update({
        "PATH": profile["path"],
        "CONDA_EXE": profile["conda"],
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "BFL_DEPLOYMENT_TARGET": target,
        "BFL_DATABASE_ENV_FILE": database_env_file,
        "BOND_ALGO_CONDA_ENV": "forecast_env",
    })
    environment.setdefault("LANG", "C.UTF-8")
    return environment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", type=Path, default=Path.cwd(), help="releases/<commit> 目录，缺省为当前目录")
    parser.add_argument("--target", choices=sorted(TARGETS))
    sub = parser.add_subparsers(dest="command", required=True)
    harness = sub.add_parser("harness", help="在绑定环境中运行 python -B -m harness ...")
    harness.add_argument("args", nargs=argparse.REMAINDER)
    probe = sub.add_parser("probe", help="只读探针")
    probe.add_argument("name", choices=sorted(_PROBES))
    probe.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        target = detect_target(args.target)
        release = args.release.resolve(strict=True)
        environment = build_environment(release, target, dict(os.environ))
    except (LaunchdReleaseError, OSError) as exc:
        print(f"release ops configuration error: {exc}", file=sys.stderr)
        return 2
    python = TARGETS[target]["python"]
    rest = [item for item in args.args if item != "--"] if args.args else []
    if args.command == "harness":
        command = [python, "-B", "-m", "harness", *rest]
    else:
        command = [python, "-B", str(release / "scripts" / "release_probes.py"), args.name, *rest]
    os.chdir(release)
    os.execve(python, command, environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
