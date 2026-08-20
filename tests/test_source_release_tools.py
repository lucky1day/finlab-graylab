from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.build_source_release import (
    MANIFEST_SCHEMA_VERSION,
    ReleaseBuildError,
    build_source_release,
)
from scripts.install_source_release import (
    ReleaseInstallError,
    install_source_release,
)


def _run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _make_source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    required_files = {
        "AGENTS.md": "# Test repository\n",
        "shared/service_instance.py": "RELEASE_TEST = True\n",
        "backend/main.py": "APP_TEST = True\n",
        "scheduler/executor.py": "EXECUTOR_TEST = True\n",
        "scripts/run_launchd_release.py": "#!/usr/bin/env python3\n",
        "scripts/tool.py": "#!/usr/bin/env python3\n",
    }
    for relative, content in required_files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (repo / "scripts" / "tool.py").chmod(0o755)
    (repo / "scripts" / "run_launchd_release.py").chmod(0o755)
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.name", "Release Test")
    _run_git(repo, "config", "user.email", "release@example.invalid")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-q", "-m", "test source")
    return repo


def test_same_clean_commit_builds_identical_release(tmp_path: Path) -> None:
    repo = _make_source_repo(tmp_path)

    first = build_source_release(repo, tmp_path / "out-a")
    second = build_source_release(repo, tmp_path / "out-b")

    assert first.commit == _run_git(repo, "rev-parse", "HEAD")
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert set(manifest) == {
        "schema_version",
        "commit",
        "root_prefix",
        "archive",
    }
    assert "tree" not in manifest
    assert manifest["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert manifest["commit"] == first.commit
    assert manifest["archive"]["sha256"] == hashlib.sha256(
        first.archive_path.read_bytes()
    ).hexdigest()


def test_installer_direct_script_cli_is_available() -> None:
    project_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "install_source_release.py"),
            "--help",
        ],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--expected-current" in completed.stdout


def test_build_rejects_dirty_repository(tmp_path: Path) -> None:
    repo = _make_source_repo(tmp_path)
    (repo / "AGENTS.md").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ReleaseBuildError, match="clean"):
        build_source_release(repo, tmp_path / "out")


def test_install_rejects_archive_checksum_mismatch(tmp_path: Path) -> None:
    repo = _make_source_repo(tmp_path)
    built = build_source_release(repo, tmp_path / "out")
    built.archive_path.write_bytes(built.archive_path.read_bytes() + b"tamper")

    with pytest.raises(ReleaseInstallError, match="SHA-256"):
        install_source_release(
            manifest_path=built.manifest_path,
            archive_path=built.archive_path,
            deploy_root=(tmp_path / "deploy").resolve(),
            runtime_root=(tmp_path / "runtime").resolve(),
            activate=False,
            expected_current=None,
            expected_archive_sha256=built.archive_sha256,
        )


def test_install_rejects_unsafe_runtime_root_before_side_effects(
    tmp_path: Path,
) -> None:
    repo = _make_source_repo(tmp_path)
    built = build_source_release(repo, tmp_path / "out")
    deploy_root = (tmp_path / "deploy").resolve()
    runtime_root = (tmp_path / "$USER" / "café").resolve()

    with pytest.raises(
        ReleaseInstallError,
        match="runtime root contains unsafe/unsupported characters",
    ):
        install_source_release(
            manifest_path=built.manifest_path,
            archive_path=built.archive_path,
            deploy_root=deploy_root,
            runtime_root=runtime_root,
            activate=False,
            expected_current=None,
            expected_archive_sha256=built.archive_sha256,
        )

    assert not deploy_root.exists()
    assert not runtime_root.exists()


@pytest.mark.parametrize("runtime_root_value", ["/", "//", "/."])
def test_install_rejects_filesystem_root_runtime_values_before_side_effects(
    tmp_path: Path,
    runtime_root_value: str,
) -> None:
    repo = _make_source_repo(tmp_path)
    built = build_source_release(repo, tmp_path / "out")
    deploy_root = (tmp_path / "deploy").resolve()

    with pytest.raises(
        ReleaseInstallError,
        match="runtime root contains unsafe/unsupported characters",
    ):
        install_source_release(
            manifest_path=built.manifest_path,
            archive_path=built.archive_path,
            deploy_root=deploy_root,
            runtime_root=runtime_root_value,
            activate=False,
            expected_current=None,
            expected_archive_sha256=built.archive_sha256,
        )

    assert not deploy_root.exists()


def test_install_rejects_manifest_commit_not_bound_to_archive(
    tmp_path: Path,
) -> None:
    repo = _make_source_repo(tmp_path)
    built = build_source_release(repo, tmp_path / "out")
    manifest = json.loads(built.manifest_path.read_text(encoding="utf-8"))
    manifest["commit"] = "a" * 40
    forged_manifest = tmp_path / "forged.manifest.json"
    forged_manifest.write_text(
        json.dumps(manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseInstallError, match="commit identity"):
        install_source_release(
            manifest_path=forged_manifest,
            archive_path=built.archive_path,
            deploy_root=(tmp_path / "deploy").resolve(),
            runtime_root=(tmp_path / "runtime").resolve(),
            activate=False,
            expected_current=None,
            expected_archive_sha256=built.archive_sha256,
        )


def test_install_rejects_obsolete_manifest_tree_field(tmp_path: Path) -> None:
    repo = _make_source_repo(tmp_path)
    built = build_source_release(repo, tmp_path / "out")
    manifest = json.loads(built.manifest_path.read_text(encoding="utf-8"))
    manifest["tree"] = "a" * 40
    forged_manifest = tmp_path / "forged.manifest.json"
    forged_manifest.write_text(
        json.dumps(manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseInstallError, match="manifest fields"):
        install_source_release(
            manifest_path=forged_manifest,
            archive_path=built.archive_path,
            deploy_root=(tmp_path / "deploy").resolve(),
            runtime_root=(tmp_path / "runtime").resolve(),
            activate=False,
            expected_current=None,
            expected_archive_sha256=built.archive_sha256,
        )


def test_install_rejects_tar_path_escape(tmp_path: Path) -> None:
    commit = "c" * 40
    archive_path = tmp_path / f"{commit}.source.tar.gz"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        payload = b"escape\n"
        member = tarfile.TarInfo("source/../escape.txt")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=archive_path.open("wb"),
        mtime=0,
    ) as compressed:
        compressed.write(buffer.getvalue())
    archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / f"{commit}.manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "commit": commit,
                "root_prefix": "source/",
                "archive": {
                    "filename": archive_path.name,
                    "sha256": archive_sha256,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseInstallError, match="unsafe archive member"):
        install_source_release(
            manifest_path=manifest_path,
            archive_path=archive_path,
            deploy_root=(tmp_path / "deploy").resolve(),
            runtime_root=(tmp_path / "runtime").resolve(),
            activate=False,
            expected_current=None,
            expected_archive_sha256=archive_sha256,
        )


def test_preinstall_then_activate_release(
    tmp_path: Path,
) -> None:
    repo = _make_source_repo(tmp_path)
    built = build_source_release(repo, tmp_path / "out")
    deploy_root = (tmp_path / "deploy").resolve()
    releases_root = deploy_root / "releases"
    old_commit = "e" * 40
    old_release = releases_root / old_commit
    old_release.mkdir(parents=True)
    (deploy_root / "current").symlink_to(
        Path("releases") / old_commit,
    )
    runtime_root = (tmp_path / "runtime").resolve()

    install_source_release(
        manifest_path=built.manifest_path,
        archive_path=built.archive_path,
        deploy_root=deploy_root,
        runtime_root=runtime_root,
        activate=False,
        expected_current=None,
        expected_archive_sha256=built.archive_sha256,
    )

    installed = install_source_release(
        manifest_path=built.manifest_path,
        archive_path=built.archive_path,
        deploy_root=deploy_root,
        runtime_root=runtime_root,
        activate=True,
        expected_current=old_commit,
        expected_archive_sha256=built.archive_sha256,
    )

    assert installed.activated is True
    assert installed.previous_commit == old_commit
    assert (deploy_root / "current").resolve() == installed.release_root
    assert (deploy_root / "previous").resolve() == old_release.resolve()
    native_cache_root = runtime_root / "cache" / "native" / built.commit
    numba_cache_root = native_cache_root / "numba"
    matplotlib_cache_root = native_cache_root / "matplotlib"
    assert (installed.release_root / ".bfl-release.env").read_text(
        encoding="utf-8"
    ) == (
        f"BFL_RELEASE_COMMIT={built.commit}\n"
        f'BFL_RUNTIME_ROOT="{runtime_root}"\n'
        f'NUMBA_CACHE_DIR="{numba_cache_root}"\n'
        f'MPLCONFIGDIR="{matplotlib_cache_root}"\n'
    )
    assert not (
        installed.release_root / "AGENTS.md"
    ).stat().st_mode & stat.S_IWUSR
    assert (installed.release_root / "scripts" / "tool.py").stat().st_mode & (
        stat.S_IXUSR
    )
    assert (runtime_root / "logs").is_dir()
    assert not (runtime_root / "logs").stat().st_mode & (
        stat.S_IWGRP | stat.S_IWOTH
    )
    events = [
        json.loads(line)
        for line in (deploy_root / "revisions.log")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["phase"] for event in events] == [
        "activation_intent",
    ]


def test_activate_rejects_current_compare_and_swap_mismatch(
    tmp_path: Path,
) -> None:
    repo = _make_source_repo(tmp_path)
    built = build_source_release(repo, tmp_path / "out")
    deploy_root = (tmp_path / "deploy").resolve()
    actual_commit = "f" * 40
    actual_release = deploy_root / "releases" / actual_commit
    actual_release.mkdir(parents=True)
    (deploy_root / "current").symlink_to(
        Path("releases") / actual_commit,
    )
    runtime_root = (tmp_path / "runtime").resolve()

    with pytest.raises(ReleaseInstallError, match="expected current"):
        install_source_release(
            manifest_path=built.manifest_path,
            archive_path=built.archive_path,
            deploy_root=deploy_root,
            runtime_root=runtime_root,
            activate=True,
            expected_current="0" * 40,
            expected_archive_sha256=built.archive_sha256,
        )

    assert (deploy_root / "current").resolve() == actual_release.resolve()
    assert not (deploy_root / "previous").exists()
    assert not runtime_root.exists()


def test_preinstalled_release_can_be_activated_after_read_only_review(
    tmp_path: Path,
) -> None:
    repo = _make_source_repo(tmp_path)
    built = build_source_release(repo, tmp_path / "out")
    deploy_root = (tmp_path / "deploy").resolve()
    runtime_root = (tmp_path / "runtime").resolve()
    old_commit = "9" * 40
    old_release = deploy_root / "releases" / old_commit
    old_release.mkdir(parents=True)
    (deploy_root / "current").symlink_to(
        Path("releases") / old_commit,
    )

    reviewed = install_source_release(
        manifest_path=built.manifest_path,
        archive_path=built.archive_path,
        deploy_root=deploy_root,
        runtime_root=runtime_root,
        activate=False,
        expected_current=None,
        expected_archive_sha256=built.archive_sha256,
    )
    activated = install_source_release(
        manifest_path=built.manifest_path,
        archive_path=built.archive_path,
        deploy_root=deploy_root,
        runtime_root=runtime_root,
        activate=True,
        expected_current=old_commit,
        expected_archive_sha256=built.archive_sha256,
    )

    assert activated.release_root == reviewed.release_root
    assert (deploy_root / "current").resolve() == reviewed.release_root


def test_activate_requires_a_preinstalled_release(tmp_path: Path) -> None:
    repo = _make_source_repo(tmp_path)
    built = build_source_release(repo, tmp_path / "out")
    deploy_root = (tmp_path / "deploy").resolve()
    old_commit = "8" * 40
    old_release = deploy_root / "releases" / old_commit
    old_release.mkdir(parents=True)
    (deploy_root / "current").symlink_to(
        Path("releases") / old_commit,
    )

    with pytest.raises(ReleaseInstallError, match="preinstalled"):
        install_source_release(
            manifest_path=built.manifest_path,
            archive_path=built.archive_path,
            deploy_root=deploy_root,
            runtime_root=(tmp_path / "runtime").resolve(),
            activate=True,
            expected_current=old_commit,
            expected_archive_sha256=built.archive_sha256,
        )

    assert (deploy_root / "current").resolve() == old_release.resolve()


def test_same_commit_activation_preserves_previous_pointer(
    tmp_path: Path,
) -> None:
    repo = _make_source_repo(tmp_path)
    built = build_source_release(repo, tmp_path / "out")
    deploy_root = (tmp_path / "deploy").resolve()
    runtime_root = (tmp_path / "runtime").resolve()
    installed = install_source_release(
        manifest_path=built.manifest_path,
        archive_path=built.archive_path,
        deploy_root=deploy_root,
        runtime_root=runtime_root,
        activate=False,
        expected_current=None,
        expected_archive_sha256=built.archive_sha256,
    )
    old_commit = "7" * 40
    old_release = deploy_root / "releases" / old_commit
    old_release.mkdir()
    (deploy_root / "current").symlink_to(
        Path("releases") / built.commit,
    )
    (deploy_root / "previous").symlink_to(
        Path("releases") / old_commit,
    )

    with pytest.raises(ReleaseInstallError, match="already current"):
        install_source_release(
            manifest_path=built.manifest_path,
            archive_path=built.archive_path,
            deploy_root=deploy_root,
            runtime_root=runtime_root,
            activate=True,
            expected_current=built.commit,
            expected_archive_sha256=built.archive_sha256,
        )

    assert (deploy_root / "current").resolve() == installed.release_root
    assert (deploy_root / "previous").resolve() == old_release.resolve()


def test_tampered_read_only_preinstalled_release_is_rejected(
    tmp_path: Path,
) -> None:
    repo = _make_source_repo(tmp_path)
    built = build_source_release(repo, tmp_path / "out")
    deploy_root = (tmp_path / "deploy").resolve()
    runtime_root = (tmp_path / "runtime").resolve()
    installed = install_source_release(
        manifest_path=built.manifest_path,
        archive_path=built.archive_path,
        deploy_root=deploy_root,
        runtime_root=runtime_root,
        activate=False,
        expected_current=None,
        expected_archive_sha256=built.archive_sha256,
    )
    old_commit = "6" * 40
    old_release = deploy_root / "releases" / old_commit
    old_release.mkdir()
    (deploy_root / "current").symlink_to(
        Path("releases") / old_commit,
    )
    backend = installed.release_root / "backend" / "main.py"
    backend.chmod(0o644)
    backend.write_text("TAMPERED = True\n", encoding="utf-8")
    backend.chmod(0o444)

    with pytest.raises(ReleaseInstallError, match="integrity"):
        install_source_release(
            manifest_path=built.manifest_path,
            archive_path=built.archive_path,
            deploy_root=deploy_root,
            runtime_root=runtime_root,
            activate=True,
            expected_current=old_commit,
            expected_archive_sha256=built.archive_sha256,
        )

    assert (deploy_root / "current").resolve() == old_release.resolve()


def test_revision_failure_keeps_current_unchanged(tmp_path: Path) -> None:
    repo = _make_source_repo(tmp_path)
    built = build_source_release(repo, tmp_path / "out")
    deploy_root = (tmp_path / "deploy").resolve()
    runtime_root = (tmp_path / "runtime").resolve()
    install_source_release(
        manifest_path=built.manifest_path,
        archive_path=built.archive_path,
        deploy_root=deploy_root,
        runtime_root=runtime_root,
        activate=False,
        expected_current=None,
        expected_archive_sha256=built.archive_sha256,
    )
    old_commit = "5" * 40
    old_release = deploy_root / "releases" / old_commit
    old_release.mkdir()
    (deploy_root / "current").symlink_to(
        Path("releases") / old_commit,
    )

    with (
        patch(
            "scripts.install_source_release._write_activation_intent",
            side_effect=OSError("disk full"),
        ),
        pytest.raises(OSError, match="disk full"),
    ):
        install_source_release(
            manifest_path=built.manifest_path,
            archive_path=built.archive_path,
            deploy_root=deploy_root,
            runtime_root=runtime_root,
            activate=True,
            expected_current=old_commit,
            expected_archive_sha256=built.archive_sha256,
        )

    assert (deploy_root / "current").resolve() == old_release.resolve()


def test_existing_insecure_runtime_root_is_rejected(tmp_path: Path) -> None:
    repo = _make_source_repo(tmp_path)
    built = build_source_release(repo, tmp_path / "out")
    runtime_root = (tmp_path / "runtime").resolve()
    runtime_root.mkdir()
    runtime_root.chmod(0o777)

    with pytest.raises(ReleaseInstallError, match="runtime root permissions"):
        install_source_release(
            manifest_path=built.manifest_path,
            archive_path=built.archive_path,
            deploy_root=(tmp_path / "deploy").resolve(),
            runtime_root=runtime_root,
            activate=False,
            expected_current=None,
            expected_archive_sha256=built.archive_sha256,
        )
