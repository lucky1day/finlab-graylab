"""日频调度使用的本机私有存储根启动契约。"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from shared.daily_coordinator_mode import resolve_daily_runtime_root


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIWEI_CACHE_ROOT_ENV = "LIWEI_0616_PHASE_A_CACHE_ROOT"


@dataclass(frozen=True)
class DailyStoragePreflight:
    """一次无歧义存储根预检的结果。"""

    labels: tuple[str, ...]


class DailyStoragePreflightError(RuntimeError):
    """日频存储根预检的稳定错误。"""

    def __init__(self, code: str, *, label: str) -> None:
        self.code = code
        self.label = label
        super().__init__(f"{code}: {label}")


def resolve_daily_storage_roots(
    *,
    project_root: str | Path = PROJECT_ROOT,
    daily_runtime_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Path]:
    """解析当前代码实际使用的七个日频私有存储根。"""
    project = Path(project_root)
    runtime = (
        resolve_daily_runtime_root()
        if daily_runtime_root is None
        else Path(daily_runtime_root)
    )
    environment = os.environ if environ is None else environ
    configured_cache = str(
        environment.get(LIWEI_CACHE_ROOT_ENV, "")
    ).strip()
    cache_root = (
        Path(configured_cache)
        if configured_cache
        else (
            project
            / "backtest_artifacts"
            / "runtime_cache"
            / "liwei_0616"
        )
    )
    return {
        "daily_runtime": runtime,
        "occurrence_locks": runtime / "occurrence-locks",
        "native_generations":
            project
            / "backtest_artifacts"
            / "input_generations"
            / "native",
        "databridge_generations":
            project
            / "backtest_artifacts"
            / "input_generations"
            / "databridge",
        "databridge_data": project / "data" / "data_bridge",
        "databridge_refresh":
            project
            / "backtest_artifacts"
            / "data_bridge_refresh",
        "liwei_cache": cache_root,
    }


def preflight_daily_storage(
    *,
    project_root: str | Path = PROJECT_ROOT,
    daily_runtime_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    service_uid: int | None = None,
) -> DailyStoragePreflight:
    """解析并预检当前日频存储根。"""
    return preflight_daily_storage_roots(
        resolve_daily_storage_roots(
            project_root=project_root,
            daily_runtime_root=daily_runtime_root,
            environ=environ,
        ),
        service_uid=service_uid,
    )


def preflight_daily_storage_roots(
    roots: Mapping[str, str | Path],
    *,
    service_uid: int | None = None,
) -> DailyStoragePreflight:
    """先只读验证全部路径，再以 0700 创建缺失目录。"""
    uid = os.getuid() if service_uid is None else service_uid
    if (
        not isinstance(uid, int)
        or isinstance(uid, bool)
        or uid < 0
    ):
        raise ValueError("daily storage service_uid is invalid")
    if not roots:
        raise ValueError("daily storage roots cannot be empty")

    normalized_roots: dict[str, Path] = {}
    for raw_label, raw_path in roots.items():
        label = str(raw_label).strip()
        if not label or label in normalized_roots:
            raise ValueError("daily storage root labels are invalid")
        path = Path(raw_path)
        if not path.is_absolute():
            raise DailyStoragePreflightError(
                "DAILY_STORAGE_PATH_UNSAFE",
                label=label,
            )
        try:
            normalized = path.resolve(strict=False)
        except OSError:
            raise DailyStoragePreflightError(
                "DAILY_STORAGE_PATH_UNSAFE",
                label=label,
            ) from None
        if normalized != path:
            raise DailyStoragePreflightError(
                "DAILY_STORAGE_PATH_UNSAFE",
                label=label,
            )
        normalized_roots[label] = path

    if len(set(normalized_roots.values())) != len(normalized_roots):
        raise ValueError("daily storage roots must use distinct paths")

    missing_paths: set[Path] = set()
    for label, path in normalized_roots.items():
        _inspect_storage_path_chain(
            path,
            label=label,
            service_uid=uid,
            missing_paths=missing_paths,
        )

    # 路径式 mkdir 会在检查与创建之间留下父目录替换窗口。创建必须从已打开的
    # 根目录 FD 开始逐级 openat/mkdirat，且绝不跟随 symlink。
    for label, path in normalized_roots.items():
        _walk_private_storage_root(
            path,
            label=label,
            service_uid=uid,
            create_missing=True,
        )

    # 创建全部完成后从 "/" 重新打开完整路径，拒绝创建过程中发生的路径替换。
    for label, path in normalized_roots.items():
        _walk_private_storage_root(
            path,
            label=label,
            service_uid=uid,
            create_missing=False,
        )

    return DailyStoragePreflight(labels=tuple(normalized_roots))


def _inspect_storage_path_chain(
    path: Path,
    *,
    label: str,
    service_uid: int,
    missing_paths: set[Path],
) -> None:
    current = path
    while True:
        try:
            details = os.stat(current, follow_symlinks=False)
        except FileNotFoundError:
            missing_paths.add(current)
        except OSError:
            raise DailyStoragePreflightError(
                "DAILY_STORAGE_PATH_UNSAFE",
                label=label,
            ) from None
        else:
            if (
                stat.S_ISLNK(details.st_mode)
                or not stat.S_ISDIR(details.st_mode)
            ):
                raise DailyStoragePreflightError(
                    "DAILY_STORAGE_PATH_UNSAFE",
                    label=label,
                )
            if current == path:
                _validate_private_root_details(
                    details,
                    label=label,
                    service_uid=service_uid,
                )
            else:
                _validate_storage_ancestor(
                    details,
                    label=label,
                    service_uid=service_uid,
                )
        if current == current.parent:
            return
        current = current.parent


def _walk_private_storage_root(
    path: Path,
    *,
    label: str,
    service_uid: int,
    create_missing: bool,
) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        current_fd = os.open("/", flags)
    except OSError:
        raise DailyStoragePreflightError(
            "DAILY_STORAGE_PATH_UNSAFE",
            label=label,
        ) from None

    try:
        root_details = os.fstat(current_fd)
        if len(path.parts) == 1:
            _validate_private_root_details(
                root_details,
                label=label,
                service_uid=service_uid,
            )
            return
        _validate_storage_ancestor(
            root_details,
            label=label,
            service_uid=service_uid,
        )

        for index, component in enumerate(path.parts[1:]):
            final_component = index == len(path.parts) - 2
            child_fd = _open_storage_component(
                current_fd,
                component,
                flags=flags,
                label=label,
                create_missing=create_missing,
            )
            try:
                details = os.fstat(child_fd)
                _require_same_directory_entry(
                    current_fd,
                    component,
                    details=details,
                    label=label,
                )
                if final_component:
                    _validate_private_root_details(
                        details,
                        label=label,
                        service_uid=service_uid,
                    )
                else:
                    _validate_storage_ancestor(
                        details,
                        label=label,
                        service_uid=service_uid,
                    )
            except BaseException:
                os.close(child_fd)
                raise
            os.close(current_fd)
            current_fd = child_fd
    finally:
        os.close(current_fd)


def _open_storage_component(
    parent_fd: int,
    component: str,
    *,
    flags: int,
    label: str,
    create_missing: bool,
) -> int:
    created = False
    try:
        child_fd = os.open(component, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create_missing:
            raise DailyStoragePreflightError(
                "DAILY_STORAGE_PATH_UNSAFE",
                label=label,
            ) from None
        try:
            os.mkdir(component, mode=0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            # 并发创建者的对象仍必须通过下面的 O_NOFOLLOW 与 inode 校验。
            pass
        except OSError:
            raise DailyStoragePreflightError(
                "DAILY_STORAGE_CREATE_FAILED",
                label=label,
            ) from None
        try:
            child_fd = os.open(
                component,
                flags,
                dir_fd=parent_fd,
            )
        except OSError:
            raise DailyStoragePreflightError(
                "DAILY_STORAGE_PATH_UNSAFE",
                label=label,
            ) from None
    except OSError:
        raise DailyStoragePreflightError(
            "DAILY_STORAGE_PATH_UNSAFE",
            label=label,
        ) from None

    if created:
        try:
            os.fchmod(child_fd, 0o700)
        except OSError:
            os.close(child_fd)
            raise DailyStoragePreflightError(
                "DAILY_STORAGE_CREATE_FAILED",
                label=label,
            ) from None
    return child_fd


def _require_same_directory_entry(
    parent_fd: int,
    component: str,
    *,
    details: os.stat_result,
    label: str,
) -> None:
    try:
        entry = os.stat(
            component,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError:
        raise DailyStoragePreflightError(
            "DAILY_STORAGE_PATH_UNSAFE",
            label=label,
        ) from None
    if (
        stat.S_ISLNK(entry.st_mode)
        or not stat.S_ISDIR(entry.st_mode)
        or entry.st_dev != details.st_dev
        or entry.st_ino != details.st_ino
    ):
        raise DailyStoragePreflightError(
            "DAILY_STORAGE_PATH_UNSAFE",
            label=label,
        )

def _validate_private_root_details(
    details: os.stat_result,
    *,
    label: str,
    service_uid: int,
) -> None:
    if details.st_uid != service_uid:
        raise DailyStoragePreflightError(
            "DAILY_STORAGE_OWNER_MISMATCH",
            label=label,
        )
    if stat.S_IMODE(details.st_mode) != 0o700:
        raise DailyStoragePreflightError(
            "DAILY_STORAGE_ROOT_NOT_PRIVATE",
            label=label,
        )


def _validate_storage_ancestor(
    details: os.stat_result,
    *,
    label: str,
    service_uid: int,
) -> None:
    if details.st_uid not in {0, service_uid}:
        raise DailyStoragePreflightError(
            "DAILY_STORAGE_OWNER_MISMATCH",
            label=label,
        )
    mode = stat.S_IMODE(details.st_mode)
    writable_by_others = bool(mode & 0o022)
    trusted_sticky_root = bool(
        details.st_uid == 0 and mode & stat.S_ISVTX
    )
    if writable_by_others and not trusted_sticky_root:
        raise DailyStoragePreflightError(
            "DAILY_STORAGE_PATH_UNSAFE",
            label=label,
        )
