from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from scripts.check_public_access import (
    PublicAccessError,
    _dashboard_gate_command,
    discover_same_origin_assets,
)


def test_discovers_every_same_origin_static_reference_in_page_order() -> None:
    html = """
    <link rel="icon" href="assets/icon.svg">
    <link rel="stylesheet" href="shell.css?v={css}">
    <img src="assets/logo.svg">
    <img src="assets/logo.svg">
    <script src="https://bond.finailab.cn/bond-factor-lab/http.js?v={js}"></script>
    <script src="https://example.invalid/ignored.js"></script>
    """.format(css="a" * 64, js="b" * 64)

    assets = discover_same_origin_assets(
        html,
        page_url="https://bond.finailab.cn/bond-factor-lab/",
    )

    assert [asset.path for asset in assets] == [
        "/bond-factor-lab/assets/icon.svg",
        "/bond-factor-lab/shell.css",
        "/bond-factor-lab/assets/logo.svg",
        "/bond-factor-lab/http.js",
    ]
    assert assets[1].version_digest == "a" * 64
    assert assets[3].version_digest == "b" * 64


@pytest.mark.parametrize(
    "reference",
    (
        "shell.js?v=short",
        "shell.js?v=" + "a" * 64 + "&v=" + "b" * 64,
        "shell.js?x=" + "a" * 64,
        "shell.js?v=%61",
        "shell.js?v=" + "a" * 64 + "#fragment",
    ),
)
def test_rejects_ambiguous_or_non_digest_asset_versions(reference: str) -> None:
    with pytest.raises(PublicAccessError):
        discover_same_origin_assets(
            f'<script src="{reference}"></script>',
            page_url="https://bond.finailab.cn/bond-factor-lab/",
        )


def test_rejects_unknown_same_origin_static_asset_type() -> None:
    with pytest.raises(PublicAccessError, match="unsupported"):
        discover_same_origin_assets(
            '<link rel="manifest" href="app.webmanifest">',
            page_url="https://bond.finailab.cn/bond-factor-lab/",
        )


def test_dashboard_gate_command_never_contains_session_token(tmp_path: Path) -> None:
    session_file = tmp_path / "session"
    args = Namespace(
        project_root=tmp_path,
        scheme_id=["first", "second"],
        session_file=session_file,
        session_fd=None,
    )

    command, pass_fds = _dashboard_gate_command(args)

    assert pass_fds == ()
    assert command.count("--scheme-id") == 2
    assert command[command.index("--api-prefix") + 1] == "/bond-factor-lab"
    assert "session-secret" not in command
    assert command[command.index("--session-file") + 1] == str(session_file)


def test_dashboard_gate_fd_is_inherited_without_secret_value(tmp_path: Path) -> None:
    args = Namespace(
        project_root=tmp_path,
        scheme_id=["first"],
        session_file=None,
        session_fd=9,
    )

    command, pass_fds = _dashboard_gate_command(args)

    assert pass_fds == (9,)
    assert command[command.index("--session-fd") + 1] == "9"
