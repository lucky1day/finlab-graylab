"""冻结 Native 日频认证的隔离真实算法 worker。"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import multiprocessing as mp
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from harness.native_daily_certification import (
    NativeCertificationItem,
    NativeDailyCertificationError,
    _generation_binding,
    _write_private_json,
    load_generation_native_matrix,
)
from scheduler.blackbox_v2_runner import (
    _PythonRuntime,
    _ResolvedReadRoot,
    _sandbox_policy,
    _sandbox_quote,
)
from scheduler.process_control import (
    ProcessGroupTerminationError,
    capture_new_session_process_group,
    terminate_process_group,
    _process_group_exists,
)
from shared import input_artifacts
from shared.daily_storage_preflight import (
    DailyStoragePreflightError,
    preflight_daily_storage_roots,
)
from shared.native_input_generation import open_native_generation


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SAFE_INHERITED_ENVIRONMENT = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
    }
)
_NATIVE_CERTIFICATION_SANDBOX_BOOTSTRAP = r'''
import ctypes
import runpy
import sys

policy = sys.argv[1]
project_root = sys.argv[2]
worker_argv = sys.argv[3:]
error = ctypes.c_char_p()
library = ctypes.CDLL("/usr/lib/libsandbox.1.dylib")
library.sandbox_init.argtypes = [
    ctypes.c_char_p,
    ctypes.c_uint64,
    ctypes.POINTER(ctypes.c_char_p),
]
library.sandbox_init.restype = ctypes.c_int
if library.sandbox_init(policy.encode("utf-8"), 0, ctypes.byref(error)) != 0:
    message = error.value.decode("utf-8", errors="replace") if error.value else "unknown error"
    raise RuntimeError(f"sandbox_init failed: {message}")
if len(worker_argv) < 2 or worker_argv[:2] != [
    "-m",
    "harness.native_daily_certification_worker",
]:
    raise RuntimeError("Native certification sandbox worker command is invalid")
sys.path.insert(0, project_root)
sys.argv = [worker_argv[1], *worker_argv[2:]]
runpy.run_module(worker_argv[1], run_name="__main__")
'''
_NATIVE_CERTIFICATION_PARENT_WATCHDOG = r'''
import os
import signal
import subprocess
import sys
import time

expected_parent_pid = int(sys.argv[1])
child = subprocess.Popen(sys.argv[2:])
while True:
    if os.getppid() != expected_parent_pid:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    returncode = child.poll()
    if returncode is not None:
        if returncode < 0:
            os.kill(os.getpid(), -returncode)
        raise SystemExit(returncode)
    time.sleep(0.05)
'''


_LIWEI_INFERENCE_BY_SCHEME = {
    "liwei_0616_10y01_cons_say_k3_div_k10":
        "run_10y01_for_feature_date",
    "liwei_0616_10y01_full_oos_k3_div_k10":
        "run_for_feature_date",
    "liwei_0616_10y02_cons_say_k3_div_k5":
        "run_10y02_for_feature_date",
    "liwei_0616_5y01_full_oos_k3_div_k10":
        "run_for_feature_date",
    "liwei_0616_5y_auc_static_all_k3_div_k10":
        "run_for_feature_date",
    "liwei_0616_5y_auc_yearly_all_k3_div_k10":
        "run_for_feature_date",
    "liwei_0616_5y_ic_yearly_all_k3_div_k10":
        "run_for_feature_date",
    "liwei_0616_7y01_cons_say_k3_div_k10":
        "run_7y01_for_feature_date",
    "liwei_0616_7y03_cons_all_k3_div_k8":
        "run_7y03_for_feature_date",
    "liwei_0616_cons_sda_k3_div_k10":
        "run_5y01_for_feature_date",
}


def run_real_native_worker(
    item: NativeCertificationItem,
    mode: str,
    *,
    generation: object,
    output_root: Path,
    policy_path: Path,
    policy_sha256: str,
    project_root: Path,
) -> Mapping[str, object]:
    """在独立进程组中执行一个真实 adapter，不调用 repository。"""
    if Path(sys.prefix).name != "forecast_env":
        raise NativeDailyCertificationError(
            "NATIVE_CERT_RUNTIME_ENV_INVALID",
            scheme_id=item.scheme_id,
        )
    run_root = (
        output_root
        / "runs"
        / _safe_path_part(item.scheme_id)
        / _safe_path_part(mode)
    )
    _make_private_tree(run_root)
    result_path = run_root / "result.json"
    cache_root = output_root / "cache"
    environment_root = output_root / "environment"
    for path in (
        cache_root,
        environment_root / "home",
        environment_root / "tmp",
        environment_root / "matplotlib",
        environment_root / "cache",
        environment_root / "config",
        environment_root / "data",
        environment_root / "numba",
        environment_root / "joblib",
    ):
        _make_private_tree(path)

    environment = _worker_environment(
        generation=generation,
        cache_root=cache_root,
        environment_root=environment_root,
    )
    command = [
        sys.executable,
        "-m",
        "harness.native_daily_certification_worker",
        "--scheme-id",
        item.scheme_id,
        "--mode",
        mode,
        "--manifest",
        str(generation.manifest_path),
        "--artifact-root",
        str(run_root / "artifacts"),
        "--result-path",
        str(result_path),
        "--policy",
        str(policy_path),
        "--policy-sha256",
        policy_sha256,
    ]
    command = _native_certification_sandbox_command(
        command,
        output_root=output_root,
        project_root=project_root,
        generation_root=Path(generation.manifest_path).parent,
    )
    try:
        completed = _run_certification_process_group(
            command,
            cwd=project_root,
            env=environment,
            timeout=item.timeout_sec,
        )
    except subprocess.CalledProcessError as exc:
        _write_private_text(
            run_root / "stdout.log",
            str(exc.stdout or ""),
        )
        _write_private_text(
            run_root / "stderr.log",
            str(exc.stderr or ""),
        )
        raise NativeDailyCertificationError(
            "NATIVE_CERT_WORKER_FAILED",
            scheme_id=item.scheme_id,
        ) from None
    except subprocess.TimeoutExpired:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_WORKER_TIMEOUT",
            scheme_id=item.scheme_id,
        ) from None
    _write_private_text(run_root / "stdout.log", completed.stdout)
    _write_private_text(run_root / "stderr.log", completed.stderr)
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_WORKER_RESULT_INVALID",
            scheme_id=item.scheme_id,
        ) from None
    if not isinstance(payload, Mapping):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_WORKER_RESULT_INVALID",
            scheme_id=item.scheme_id,
        )
    return payload


def execute_native_certification_worker(
    *,
    scheme_id: str,
    mode: str,
    manifest_path: str | Path,
    artifact_root: str | Path,
    result_path: str | Path,
    policy_path: str | Path,
    policy_sha256: str,
) -> dict[str, object]:
    """安装网络 deny 后执行真实 predict.run。"""
    policy = Path(policy_path)
    try:
        actual_policy_sha256 = hashlib.sha256(
            policy.read_bytes()
        ).hexdigest()
    except OSError:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_POLICY_DRIFT",
            scheme_id=scheme_id,
        ) from None
    if actual_policy_sha256 != policy_sha256:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_POLICY_DRIFT",
            scheme_id=scheme_id,
        )
    _require_spawn_multiprocessing()
    matrix = {
        item.scheme_id: item
        for item in load_generation_native_matrix(policy)
    }
    item = matrix.get(scheme_id)
    if item is None:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_SCHEME_NOT_ALLOWED",
            scheme_id=scheme_id,
        )
    expected_modes = (
        {"cold", "warm_build", "warm_hit"}
        if item.is_liwei
        else {"first", "second"}
    )
    if mode not in expected_modes:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_MODE_INVALID",
            scheme_id=scheme_id,
        )
    manifest = Path(manifest_path)
    artifacts = Path(artifact_root)
    result = Path(result_path)
    if (
        not manifest.is_absolute()
        or not artifacts.is_absolute()
        or not result.is_absolute()
    ):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_WORKER_PATH_UNSAFE",
            scheme_id=scheme_id,
        )
    _make_private_tree(artifacts)
    generation = open_native_generation(
        manifest,
        expected_generation_id=os.environ.get(
            input_artifacts.NATIVE_GENERATION_ID_ENV
        ),
        expected_manifest_sha256=os.environ.get(
            input_artifacts.NATIVE_MANIFEST_SHA256_ENV
        ),
        expected_business_date=os.environ.get(
            input_artifacts.NATIVE_BUSINESS_DATE_ENV
        ),
        expected_feature_date=os.environ.get(
            input_artifacts.NATIVE_FEATURE_DATE_ENV
        ),
    )
    network_attempts = [0]
    with _network_denied(network_attempts):
        records = _run_real_predict_entry(
            item,
            mode=mode,
            predict_date=generation.business_date,
            artifact_root=artifacts,
        )
    payload: dict[str, object] = {
        "scheme_id": item.scheme_id,
        "mode": mode,
        "records": records,
        "generation_id": generation.generation_id,
        "manifest_sha256": generation.manifest_sha256,
        "network_attempts": network_attempts[0],
        "sandbox_profile": "native-certification-macos-v1",
        "multiprocessing_start_method": "spawn",
        "persistence": "none",
        "cache_status": _cache_status(records),
    }
    _write_private_json(result, payload)
    return payload


def _run_real_predict_entry(
    item: NativeCertificationItem,
    *,
    mode: str,
    predict_date: str,
    artifact_root: Path,
) -> list[dict[str, object]]:
    predict = importlib.import_module(
        f"schemes.{item.scheme_id}.predict"
    )
    with ExitStack() as stack:
        for builder_name in (
            "build_daily_input_artifact",
            "build_weekly_input_artifact",
            "build_monthly_input_artifact",
        ):
            original = getattr(predict, builder_name, None)
            if original is None:
                continue

            def isolated_builder(
                *args: object,
                _original: Callable[..., object] = original,
                **kwargs: object,
            ) -> object:
                kwargs["output_root"] = artifact_root
                return _original(*args, **kwargs)

            stack.enter_context(
                _replace_attribute(
                    predict,
                    builder_name,
                    isolated_builder,
                )
            )

        if item.is_liwei and mode == "cold":
            inference_name = _LIWEI_INFERENCE_BY_SCHEME[
                item.scheme_id
            ]
            original_inference = getattr(predict, inference_name)

            def cold_inference(
                *args: object,
                **kwargs: object,
            ) -> object:
                kwargs["use_incremental_cache"] = False
                kwargs["cache_root"] = None
                return original_inference(*args, **kwargs)

            stack.enter_context(
                _replace_attribute(
                    predict,
                    inference_name,
                    cold_inference,
                )
            )
        raw_records = predict.run(predict_date)
    if not isinstance(raw_records, list):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_RECORDS_INVALID",
            scheme_id=item.scheme_id,
        )
    return [
        asdict(record)
        if hasattr(record, "__dataclass_fields__")
        else dict(record)
        for record in raw_records
    ]


def _worker_environment(
    *,
    generation: object,
    cache_root: Path,
    environment_root: Path,
) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in _SAFE_INHERITED_ENVIRONMENT
        if name in os.environ
    }
    binding = _generation_binding(generation)
    environment.update(
        {
            input_artifacts.NATIVE_INPUT_MODE_ENV:
                input_artifacts.NATIVE_INPUT_MODE,
            input_artifacts.NATIVE_MANIFEST_PATH_ENV:
                str(generation.manifest_path),
            input_artifacts.NATIVE_GENERATION_ID_ENV:
                binding["generation_id"],
            input_artifacts.NATIVE_MANIFEST_SHA256_ENV:
                binding["manifest_sha256"],
            input_artifacts.NATIVE_BUSINESS_DATE_ENV:
                binding["business_date"],
            input_artifacts.NATIVE_FEATURE_DATE_ENV:
                binding["feature_date"],
            "BOND_DAILY_COORDINATOR_MODE": "legacy",
            "LIWEI_0616_PHASE_A_CACHE_ROOT": str(cache_root),
            "HOME": str(environment_root / "home"),
            "TMPDIR": str(environment_root / "tmp"),
            "MPLCONFIGDIR": str(environment_root / "matplotlib"),
            "XDG_CACHE_HOME": str(environment_root / "cache"),
            "XDG_CONFIG_HOME": str(environment_root / "config"),
            "XDG_DATA_HOME": str(environment_root / "data"),
            "NUMBA_CACHE_DIR": str(environment_root / "numba"),
            "JOBLIB_TEMP_FOLDER": str(environment_root / "joblib"),
            "PATH": (
                f"{Path(sys.executable).resolve().parent}:"
                "/usr/bin:/bin"
            ),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
        }
    )
    return environment


def _native_certification_sandbox_command(
    command: list[str],
    *,
    output_root: Path,
    project_root: Path,
    generation_root: Path,
    python_executable: Path | None = None,
    python_prefix: Path | None = None,
    sandbox_available: Callable[[], bool] | None = None,
) -> list[str]:
    """使用与 Blackbox runner 同源的 macOS policy 包裹 Native worker。"""
    available = sandbox_available or (
        lambda: shutil.which("sandbox-exec") is not None
    )
    if not available():
        raise NativeDailyCertificationError(
            "NATIVE_CERT_OS_SANDBOX_UNAVAILABLE"
        )
    executable = Path(
        python_executable or command[0]
    ).resolve(strict=True)
    prefix = Path(
        python_prefix or sys.prefix
    ).resolve(strict=True)
    project = project_root.resolve(strict=True)
    generation = generation_root.resolve(strict=True)
    writable = output_root.resolve(strict=True)
    if Path(command[0]).resolve(strict=True) != executable:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_RUNTIME_ENV_INVALID"
        )
    read_roots = [
        _ResolvedReadRoot(project, project, project),
        _ResolvedReadRoot(generation, generation, generation),
    ]
    for system_runtime_root in (
        Path("/usr/lib"),
        Path(
            "/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld"
        ),
    ):
        if system_runtime_root.exists():
            resolved_system_root = system_runtime_root.resolve(strict=True)
            read_roots.append(
                _ResolvedReadRoot(
                    system_runtime_root,
                    resolved_system_root,
                    resolved_system_root.parent,
                )
            )
    libomp = Path("/opt/homebrew/opt/libomp/lib")
    if libomp.exists():
        resolved_libomp = libomp.resolve(strict=True)
        read_roots.append(
            _ResolvedReadRoot(
                libomp,
                resolved_libomp,
                resolved_libomp.parent,
            )
        )
    policy = _sandbox_policy(
        writable=writable,
        runtime=_PythonRuntime(executable, prefix),
        read_roots=tuple(read_roots),
        script_path=None,
        input_path=None,
        data_dir=None,
    )
    policy = "\n".join(
        (
            policy,
            "(allow process-fork)",
            "(allow ipc-posix-sem)",
            "(allow process-exec",
            f'  (literal "{_sandbox_quote(executable)}")',
            ")",
            "(allow file-read-data file-read-metadata file-test-existence",
            '  (literal "/")',
            '  (literal "/dev/fd")',
            '  (subpath "/dev/fd")',
            ")",
            "(allow sysctl-read",
            '  (sysctl-name "kern.bootargs"',
            '               "security.mac.lockdown_mode_state")',
            ")",
        )
    )
    return [
        str(executable),
        "-I",
        "-c",
        _NATIVE_CERTIFICATION_SANDBOX_BOOTSTRAP,
        policy,
        str(project),
        *command[1:],
    ]


def _require_spawn_multiprocessing() -> None:
    """要求与当前 forecast_env 生产运行时相同的 spawn 拓扑。"""
    if mp.get_start_method(allow_none=False) != "spawn":
        raise NativeDailyCertificationError(
            "NATIVE_CERT_MULTIPROCESSING_UNSAFE"
        )


def _run_certification_process_group(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """执行 sandbox worker，并在任何退出路径确认整个进程组消失。"""
    process = subprocess.Popen(
        [
            command[0],
            "-I",
            "-c",
            _NATIVE_CERTIFICATION_PARENT_WATCHDOG,
            str(os.getpid()),
            *command,
        ],
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_group_id = capture_new_session_process_group(process)
    cleanup_confirmed = False
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            termination = terminate_process_group(
                process,
                process_group_id=process_group_id,
            )
            if not termination.confirmed_gone:
                raise ProcessGroupTerminationError(
                    termination=termination,
                    context=(
                        "Native certification worker timed out"
                    ),
                ) from exc
            cleanup_confirmed = True
            raise
        if _process_group_exists(process_group_id):
            termination = terminate_process_group(
                process,
                process_group_id=process_group_id,
            )
            if not termination.confirmed_gone:
                raise ProcessGroupTerminationError(
                    termination=termination,
                    context=(
                        "Native certification worker left descendants"
                    ),
                )
            cleanup_confirmed = True
            raise NativeDailyCertificationError(
                "NATIVE_CERT_ORPHAN_PROCESS"
            )
    except BaseException:
        if (
            not cleanup_confirmed
            and _process_group_exists(process_group_id)
        ):
            termination = terminate_process_group(
                process,
                process_group_id=process_group_id,
            )
            if not termination.confirmed_gone:
                raise ProcessGroupTerminationError(
                    termination=termination,
                    context=(
                        "Native certification worker cleanup failed"
                    ),
                )
        raise
    completed = subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout,
        stderr,
    )
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            command,
            output=stdout,
            stderr=stderr,
        )
    return completed


@contextmanager
def _network_denied(counter: list[int]) -> Iterator[None]:
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection

    def deny(*_args: object, **_kwargs: object) -> None:
        counter[0] += 1
        raise OSError("network disabled by Native certification")

    socket.socket.connect = deny
    socket.create_connection = deny
    try:
        yield
    finally:
        socket.socket.connect = original_connect
        socket.create_connection = original_create_connection


@contextmanager
def _replace_attribute(
    target: object,
    name: str,
    replacement: object,
) -> Iterator[None]:
    original = getattr(target, name)
    setattr(target, name, replacement)
    try:
        yield
    finally:
        setattr(target, name, original)


def _cache_status(
    records: Sequence[Mapping[str, object]],
) -> str | None:
    statuses = {
        str(extra.get("phase_a_cache_status"))
        for record in records
        if isinstance((extra := record.get("extra")), Mapping)
        and extra.get("phase_a_cache_status") is not None
    }
    if not statuses:
        return None
    if len(statuses) != 1:
        return "mixed"
    return statuses.pop()


def _write_private_text(path: Path, value: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _make_private_tree(path: Path) -> None:
    current = path
    while True:
        try:
            details = current.lstat()
        except FileNotFoundError:
            if current == current.parent:
                raise NativeDailyCertificationError(
                    "NATIVE_CERT_OUTPUT_ROOT_UNSAFE"
                ) from None
            current = current.parent
            continue
        except OSError:
            raise NativeDailyCertificationError(
                "NATIVE_CERT_OUTPUT_ROOT_UNSAFE"
            ) from None
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise NativeDailyCertificationError(
                "NATIVE_CERT_OUTPUT_ROOT_UNSAFE"
            )
        break
    try:
        preflight_daily_storage_roots(
            {"native_certification": path}
        )
    except DailyStoragePreflightError:
        raise NativeDailyCertificationError(
            "NATIVE_CERT_OUTPUT_ROOT_UNSAFE"
        ) from None


def _safe_path_part(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(
            character
            not in (
                "abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            )
            for character in value
        )
    ):
        raise NativeDailyCertificationError(
            "NATIVE_CERT_WORKER_PATH_UNSAFE"
        )
    return value


def _parse_arguments(
    argv: Sequence[str] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one isolated Native certification worker."
    )
    parser.add_argument("--scheme-id", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--result-path", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--policy-sha256", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_arguments(argv)
    execute_native_certification_worker(
        scheme_id=args.scheme_id,
        mode=args.mode,
        manifest_path=args.manifest,
        artifact_root=args.artifact_root,
        result_path=args.result_path,
        policy_path=args.policy,
        policy_sha256=args.policy_sha256,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
