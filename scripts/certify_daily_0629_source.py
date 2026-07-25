"""0629 日频真实执行认证的可信控制进程入口。"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


sys.dont_write_bytecode = True
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ISOLATION_ENV = "BFL_DAILY_0629_CERT_CONTROL_ISOLATION"


def _spawn_isolated_control_process() -> int:
    """在 import 候选模块前创建空 pycache root，并等待可信子进程。"""
    with tempfile.TemporaryDirectory(
        prefix="bfl-daily-0629-cert-control-pycache-"
    ) as pycache_root_value:
        pycache_root = Path(pycache_root_value).resolve(strict=True)
        os.chmod(pycache_root, 0o700)
        details = pycache_root.lstat()
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700
            or any(pycache_root.iterdir())
        ):
            return 1
        environment = dict(os.environ)
        for name in (
            "PYTHONHOME",
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "PYTHONUSERBASE",
        ):
            environment.pop(name, None)
        environment["PYTHONPYCACHEPREFIX"] = str(pycache_root)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        environment[CONTROL_ISOLATION_ENV] = (
            f"{details.st_dev}:{details.st_ino}"
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-s",
                "-B",
                str(Path(__file__).resolve()),
                *sys.argv[1:],
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
        )
        return int(completed.returncode)


def _run_isolated_control_process() -> int:
    """仅在启动期 pycache 隔离已生效后 import 候选认证代码。"""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from harness.daily_0629_certification import (
        _require_control_process_isolation,
        main,
    )

    _require_control_process_isolation()
    return int(main())


if __name__ == "__main__":
    if os.environ.get(CONTROL_ISOLATION_ENV):
        raise SystemExit(_run_isolated_control_process())
    raise SystemExit(_spawn_isolated_control_process())
