"""Read-only verification for standalone embedded 7Y deliveries."""

from __future__ import annotations

import ast
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from dataclasses import asdict
from datetime import date
import hashlib
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from typing import Iterable

from tools.embedded_7y_blackbox.frozen_schemes import SCHEMES, FrozenScheme
from tools.embedded_7y_blackbox.renderer import render_metadata


BLACKBOX_PYTHON = Path(
    "/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python"
)
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
PLATFORM_FILENAMES = (
    "daily_output.csv",
    "weekly_output.csv",
    "monthly_output.csv",
    "api_wind_date.csv",
)
REQUEST_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "daily_cutoff_key",
    "weekly_cutoff_key",
    "monthly_cutoff_key",
)
RESULT_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
)
AUDIT_FIELDS = {
    "phase",
    "candidate_id",
    "config_hash",
    "feature_date",
    "target_date",
    "label",
    "action",
}
PHASE_COUNTS = {"SIM": 117, "REAL": 203}
MAX_SCRIPT_BYTES = 50 * 1024 * 1024
MAX_PROJECTED_BYTES = 64 * 1024 * 1024
FROZEN_METRICS = {
    "sim_accuracy": 0.6262626262626263,
    "sim_trade_rate": 0.8461538461538461,
    "real_accuracy": 0.60431654676259,
    "real_trade_rate": 0.6847290640394089,
    "combined_accuracy": 0.6134453781512605,
    "combined_trade_rate": 0.74375,
}
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_PERIOD_PATTERN = re.compile(r"[0-9]{6}\Z")
_WINDOWS_ABSOLUTE_PATTERN = re.compile(r"[A-Za-z]:[\\/]")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AssertionError(f"duplicate JSON metadata member: {key}")
        result[key] = value
    return result


def _scheme_for_delivery(delivery: Path) -> FrozenScheme:
    try:
        return SCHEMES[delivery.name]
    except KeyError as error:
        raise AssertionError(f"unknown delivery scheme: {delivery.name}") from error


def _exact_pair(delivery: Path) -> tuple[Path, Path]:
    delivery = Path(delivery)
    if delivery.is_symlink():
        raise AssertionError(f"delivery symlink is forbidden: {delivery}")
    try:
        mode = delivery.lstat().st_mode
    except FileNotFoundError as error:
        raise AssertionError(f"delivery does not exist: {delivery}") from error
    if not stat.S_ISDIR(mode):
        raise AssertionError(f"delivery must be a directory: {delivery}")
    expected = {
        f"{delivery.name}.py",
        f"{delivery.name}.json",
    }
    entries = {entry.name: entry for entry in delivery.iterdir()}
    if set(entries) != expected:
        raise AssertionError(
            f"delivery must contain exact .py + .json pair: {sorted(entries)}"
        )
    for entry in entries.values():
        entry_mode = entry.lstat().st_mode
        if entry.is_symlink():
            raise AssertionError(f"delivery symlink is forbidden: {entry}")
        if not stat.S_ISREG(entry_mode):
            raise AssertionError(f"delivery entry must be a regular file: {entry}")
    return entries[f"{delivery.name}.py"], entries[f"{delivery.name}.json"]


def _payload_constant_ids(tree: ast.AST) -> set[int]:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(
            isinstance(target, ast.Name) and target.id == "_PAYLOAD_MANIFEST"
            for target in targets
        ):
            return {
                id(child)
                for child in ast.walk(node.value)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            }
    return set()


def verify_no_forbidden_paths(source: str) -> None:
    """Reject absolute filesystem dependencies from Python source."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise AssertionError(f"runner AST parse failed: {error}") from error
    payload_constants = _payload_constant_ids(tree)
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Constant)
            or not isinstance(node.value, str)
            or id(node) in payload_constants
        ):
            continue
        value = node.value
        is_posix_absolute = value.startswith("/") and value != "/"
        if is_posix_absolute or _WINDOWS_ABSOLUTE_PATTERN.match(value):
            raise AssertionError(f"absolute dependency is forbidden: {value}")


def _qualified_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


class _ForbiddenAstVisitor(ast.NodeVisitor):
    _FORBIDDEN_NAMES = {
        "__import__",
        "breakpoint",
        "compile",
        "eval",
        "exec",
    }
    _FORBIDDEN_MODULES = {
        "ftplib",
        "http",
        "requests",
        "socket",
        "sqlite3",
        "subprocess",
        "urllib",
    }
    _FORBIDDEN_QUALIFIED = {
        "builtins.__import__",
        "builtins.breakpoint",
        "builtins.compile",
        "builtins.eval",
        "builtins.exec",
        "os.fork",
        "os.popen",
        "os.system",
    }
    _FORBIDDEN_OS_PREFIXES = (
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
    )

    def __init__(self) -> None:
        self._aliases: dict[str, str] = {}

    def _resolved_name(self, node: ast.AST) -> str:
        name = _qualified_name(node)
        root, separator, suffix = name.partition(".")
        resolved_root = self._aliases.get(root, root)
        return (
            f"{resolved_root}.{suffix}"
            if separator
            else resolved_root
        )

    def visit_Call(self, node: ast.Call) -> None:
        name = self._resolved_name(node.func)
        if (
            name in self._FORBIDDEN_NAMES
            or name in self._FORBIDDEN_QUALIFIED
            or name.startswith(self._FORBIDDEN_OS_PREFIXES)
            or any(
                name == module or name.startswith(f"{module}.")
                for module in self._FORBIDDEN_MODULES
            )
        ):
            raise AssertionError(f"forbidden call in runner: {name}")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            if root in self._FORBIDDEN_MODULES:
                raise AssertionError(f"forbidden call dependency in runner: {root}")
            bound_name = alias.asname or root
            self._aliases[bound_name] = alias.name if alias.asname else root
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        root = module.split(".", 1)[0]
        if root in self._FORBIDDEN_MODULES:
            raise AssertionError(f"forbidden call dependency in runner: {root}")
        for alias in node.names:
            if alias.name == "*" and root == "os":
                raise AssertionError("forbidden call dependency in runner: os.*")
            bound_name = alias.asname or alias.name
            self._aliases[bound_name] = f"{module}.{alias.name}"
        self.generic_visit(node)


def _manifest_from_tree(tree: ast.AST) -> list[dict[str, object]]:
    for node in tree.body if isinstance(tree, ast.Module) else ():
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "_PAYLOAD_MANIFEST"
            for target in node.targets
        ):
            continue
        try:
            manifest = ast.literal_eval(node.value)
        except (TypeError, ValueError) as error:
            raise AssertionError("payload manifest must be a literal") from error
        if not isinstance(manifest, list):
            raise AssertionError("payload manifest must be a list")
        return manifest
    raise AssertionError("payload manifest is missing")


def _frozen_scheme_from_tree(tree: ast.AST) -> dict[str, object]:
    for node in tree.body if isinstance(tree, ast.Module) else ():
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "FROZEN_SCHEME"
            for target in node.targets
        ):
            continue
        try:
            frozen_scheme = ast.literal_eval(node.value)
        except (TypeError, ValueError) as error:
            raise AssertionError("frozen scheme must be a literal") from error
        if not isinstance(frozen_scheme, dict):
            raise AssertionError("frozen scheme must be an object")
        return frozen_scheme
    raise AssertionError("frozen scheme is missing")


def _decoded_entry_size(
    entry: dict[str, object],
    *,
    remaining_projection: int,
) -> int:
    encoded_chunks = entry["encoded_chunks"]
    assert isinstance(encoded_chunks, list)
    try:
        compressed = base64.b85decode(
            "".join(encoded_chunks).encode("ascii")
        )
    except (UnicodeEncodeError, ValueError) as error:
        raise AssertionError("payload Base85 encoding is invalid") from error
    compressed_sha256 = entry["compressed_sha256"]
    if hashlib.sha256(compressed).hexdigest() != compressed_sha256:
        raise AssertionError("payload integrity: compressed SHA256 mismatch")

    decoder = lzma.LZMADecompressor()
    raw_digest = hashlib.sha256()
    raw_size = 0
    pending = compressed
    while True:
        remaining = remaining_projection - raw_size
        if remaining <= 0:
            raise AssertionError("projected extraction size exceeds 64 MiB")
        try:
            chunk = decoder.decompress(
                pending,
                max_length=min(1_048_576, remaining),
            )
        except lzma.LZMAError as error:
            raise AssertionError("payload integrity: invalid LZMA stream") from error
        pending = b""
        raw_digest.update(chunk)
        raw_size += len(chunk)
        if raw_size >= remaining_projection:
            raise AssertionError("projected extraction size exceeds 64 MiB")
        if decoder.eof:
            if decoder.unused_data:
                raise AssertionError("payload integrity: trailing compressed data")
            break
        if decoder.needs_input:
            raise AssertionError("payload integrity: truncated LZMA stream")
    if (
        raw_size != entry["raw_size"]
        or raw_digest.hexdigest() != entry["raw_sha256"]
    ):
        raise AssertionError("payload integrity: raw size or SHA256 mismatch")
    return raw_size


def _projected_payload_size(
    tree: ast.AST,
    *,
    base_projection: int,
) -> int:
    manifest = _manifest_from_tree(tree)
    expected_fields = {
        "relative_path",
        "raw_size",
        "raw_sha256",
        "compressed_sha256",
        "encoded_chunks",
    }
    total = 0
    seen: set[str] = set()
    for entry in manifest:
        if not isinstance(entry, dict) or set(entry) != expected_fields:
            raise AssertionError("payload manifest entry fields are invalid")
        relative_path = entry["relative_path"]
        raw_size = entry["raw_size"]
        raw_sha256 = entry["raw_sha256"]
        compressed_sha256 = entry["compressed_sha256"]
        encoded_chunks = entry["encoded_chunks"]
        if not isinstance(relative_path, str):
            raise AssertionError("payload relative path must be text")
        path = PurePosixPath(relative_path)
        if (
            not relative_path
            or path.is_absolute()
            or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in path.parts)
            or relative_path in seen
        ):
            raise AssertionError(f"unsafe payload manifest path: {relative_path!r}")
        if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 0:
            raise AssertionError("payload raw_size must be a nonnegative integer")
        if (
            not isinstance(raw_sha256, str)
            or not _HASH_PATTERN.fullmatch(raw_sha256)
            or not isinstance(compressed_sha256, str)
            or not _HASH_PATTERN.fullmatch(compressed_sha256)
        ):
            raise AssertionError("payload manifest hash is invalid")
        if (
            not isinstance(encoded_chunks, list)
            or not encoded_chunks
            or not all(isinstance(chunk, str) for chunk in encoded_chunks)
        ):
            raise AssertionError("payload encoded chunks are invalid")
        seen.add(relative_path)
        decoded_size = _decoded_entry_size(
            entry,
            remaining_projection=MAX_PROJECTED_BYTES - base_projection - total,
        )
        total += decoded_size
    return total


def verify_structure(delivery: Path) -> None:
    """Validate one exact standalone delivery without executing it."""
    delivery = Path(delivery)
    scheme = _scheme_for_delivery(delivery)
    runner_path, metadata_path = _exact_pair(delivery)
    if stat.S_IMODE(delivery.lstat().st_mode) != 0o700:
        raise AssertionError("delivery directory mode must be 0700")
    for path in (runner_path, metadata_path):
        if stat.S_IMODE(path.lstat().st_mode) != 0o600:
            raise AssertionError(f"delivery file mode must be 0600: {path.name}")

    try:
        metadata = json.loads(
            metadata_path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AssertionError(f"metadata JSON is invalid: {error}") from error
    expected_metadata = render_metadata(scheme)
    if metadata != expected_metadata:
        raise AssertionError("metadata does not match the frozen scheme exactly")

    script_size = runner_path.stat().st_size
    if script_size >= MAX_SCRIPT_BYTES:
        raise AssertionError("runner script exceeds the 50 MiB size limit")
    try:
        source = runner_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise AssertionError("runner must be UTF-8 Python source") from error
    try:
        tree = ast.parse(source, filename=runner_path.name)
    except SyntaxError as error:
        raise AssertionError(f"runner AST parse failed: {error}") from error
    verify_no_forbidden_paths(source)
    _ForbiddenAstVisitor().visit(tree)
    expected_frozen_scheme = json.loads(
        json.dumps(asdict(scheme), sort_keys=True, separators=(",", ":"))
    )
    if _frozen_scheme_from_tree(tree) != expected_frozen_scheme:
        raise AssertionError("embedded frozen scheme identity does not match delivery")
    base_projection = script_size + metadata_path.stat().st_size
    projected_size = (
        base_projection
        + _projected_payload_size(
            tree,
            base_projection=base_projection,
        )
    )
    if projected_size >= MAX_PROJECTED_BYTES:
        raise AssertionError(
            f"projected extraction size exceeds 64 MiB: {projected_size}"
        )


def _canonical_date(value: str, label: str) -> date:
    if not isinstance(value, str):
        raise AssertionError(f"{label} must be text")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise AssertionError(f"{label} must be YYYY-MM-DD: {value!r}") from error
    if parsed.isoformat() != value:
        raise AssertionError(f"{label} must be canonical YYYY-MM-DD: {value!r}")
    return parsed


def _read_calendar(data_dir: Path) -> tuple[dict[str, str], list[str]]:
    path = Path(data_dir) / "api_wind_date.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != ("rdate", "week_id"):
            raise AssertionError("api_wind_date.csv must contain exact rdate,week_id")
        rows = list(reader)
    if not rows:
        raise AssertionError("api_wind_date.csv must not be empty")
    mapping: dict[str, str] = {}
    ordered_dates: list[str] = []
    previous: date | None = None
    for row in rows:
        rdate = row["rdate"]
        parsed = _canonical_date(rdate, "api_wind_date.rdate")
        week_id = row["week_id"]
        if not _PERIOD_PATTERN.fullmatch(week_id):
            raise AssertionError("api_wind_date.week_id must be six digits")
        if rdate in mapping or (previous is not None and parsed <= previous):
            raise AssertionError("api_wind_date.rdate must be unique and increasing")
        mapping[rdate] = week_id
        ordered_dates.append(rdate)
        previous = parsed
    return mapping, ordered_dates


def _independence_request(fixture_root: Path) -> dict[str, str]:
    calendar, calendar_dates = _read_calendar(fixture_root)
    daily_path = Path(fixture_root) / "daily_output.csv"
    with daily_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if "date" not in tuple(reader.fieldnames or ()):
            raise AssertionError("daily_output.csv must contain date")
        daily_dates = [row["date"] for row in reader]
    if not daily_dates:
        raise AssertionError("daily_output.csv must not be empty")
    canonical_daily = sorted(
        {_canonical_date(value, "daily_output.date").isoformat() for value in daily_dates}
    )
    later_by_date = {
        value: calendar_dates[index + 1]
        for index, value in enumerate(calendar_dates[:-1])
    }
    eligible = [
        value
        for value in canonical_daily
        if value in calendar and value in later_by_date
    ]
    if not eligible:
        raise AssertionError(
            "fixtures need a daily feature date with calendar target coverage"
        )
    feature_date = eligible[-1]
    return {
        "request_id": "independence-001",
        "predict_date": feature_date,
        "feature_date": feature_date,
        "target_date": later_by_date[feature_date],
        "daily_cutoff_key": feature_date,
        "weekly_cutoff_key": calendar[feature_date],
        "monthly_cutoff_key": feature_date.replace("-", "")[:6],
    }


_AUDIT_HOOK_SOURCE = """\
import json
import os
import sys

_trace_descriptor = int(os.environ["EMBEDDED_7Y_TRACE_FD"])
_path_events = {
    "open",
    "os.chdir",
    "os.listdir",
    "os.mkdir",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.scandir",
    "os.truncate",
}

def _trace_hook(event, args):
    if event not in _path_events or not args:
        return
    write_access = event in {
        "os.mkdir",
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.truncate",
    }
    if event == "open":
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        if isinstance(mode, str):
            write_access = any(token in mode for token in "wax+")
        if isinstance(flags, int):
            write_access = write_access or bool(
                flags
                & (
                    os.O_WRONLY
                    | os.O_RDWR
                    | os.O_CREAT
                    | os.O_TRUNC
                    | os.O_APPEND
                )
            )
    candidates = args[:2] if event == "os.rename" else args[:1]
    for candidate in candidates:
        if not isinstance(candidate, (str, bytes, os.PathLike)):
            continue
        try:
            path = os.fsdecode(candidate)
            payload = json.dumps(
                {
                    "event": event,
                    "path": path,
                    "write": write_access,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8") + b"\\n"
            os.write(_trace_descriptor, payload)
        except Exception:
            pass

sys.addaudithook(_trace_hook)
"""


def _forbidden_runtime_roots(delivery: Path) -> tuple[Path, ...]:
    repository_roots = {
        _PROJECT_ROOT,
        Path.home() / "bond-factor-lab",
    }
    roots: set[Path] = set()
    for root in repository_roots:
        roots.add(
            root
            / "source_evidence/benchmark_batches/daily_0629/source_package"
        )
        roots.add(root / "schemes/daily_5y_lgbm_5y10_0629")
        roots.add(root / "schemes/daily_10y_lgbm_10y04_0629")
    roots.add(Path.home() / "Desktop/方案/0629/forecast_project")
    for scheme_id in SCHEMES:
        if scheme_id != delivery.name:
            roots.add(delivery.parent / scheme_id)
    return tuple(sorted((root.resolve() for root in roots), key=str))


def _assert_no_forbidden_access(
    trace_records: Iterable[dict[str, object]],
    *,
    cwd: Path,
    forbidden_roots: Iterable[Path],
    write_root: Path,
) -> None:
    records = list(trace_records)
    if not records:
        raise AssertionError("delivery file-access trace was not produced")
    write_root = write_root.resolve()
    for record in records:
        raw_path = record.get("path")
        if not isinstance(raw_path, str) or raw_path.startswith("<"):
            continue
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        resolved = candidate.resolve()
        for root in forbidden_roots:
            if resolved == root or root in resolved.parents:
                raise AssertionError(
                    f"forbidden root access: {record['event']} {resolved}"
                )
        if bool(record.get("write")) and not (
            resolved == write_root or write_root in resolved.parents
        ):
            raise AssertionError(
                f"forbidden write access: {record['event']} {resolved}"
            )


def _sandbox_profile(
    *,
    write_root: Path,
    forbidden_roots: Iterable[Path],
) -> str:
    rules = [
        "(version 1)",
        "(deny default)",
        "(allow process*)",
        "(allow file-read*)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix*)",
        "(allow file-ioctl)",
        "(allow signal)",
        (
            "(allow file-write* (subpath "
            f"{json.dumps(str(write_root.resolve()), ensure_ascii=False)}))"
        ),
    ]
    for root in forbidden_roots:
        literal = json.dumps(str(root), ensure_ascii=False)
        rules.append(f"(deny file-read* (subpath {literal}))")
    return "\n".join(rules) + "\n"


def _run_confined_process(
    arguments: list[str],
    *,
    cwd: Path,
    write_root: Path,
    forbidden_roots: tuple[Path, ...] = (),
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
    cwd = cwd.resolve()
    write_root = write_root.resolve()
    write_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    control_root = write_root.parent / f".{write_root.name}-control"
    control_root.mkdir(mode=0o700, exist_ok=False)
    trace_root = control_root / "tracer"
    trace_root.mkdir(mode=0o700)
    sitecustomize = trace_root / "sitecustomize.py"
    sitecustomize.write_text(_AUDIT_HOOK_SOURCE, encoding="utf-8")
    sitecustomize.chmod(0o400)
    sandbox_profile = control_root / "delivery.sb"
    sandbox_profile.write_text(
        _sandbox_profile(
            write_root=write_root,
            forbidden_roots=forbidden_roots,
        ),
        encoding="utf-8",
    )
    sandbox_profile.chmod(0o400)
    trace_read, trace_write = os.pipe()
    os.set_inheritable(trace_write, True)
    trace_chunks: list[bytes] = []

    def collect_trace() -> None:
        while True:
            chunk = os.read(trace_read, 65_536)
            if not chunk:
                return
            trace_chunks.append(chunk)

    collector = threading.Thread(target=collect_trace, daemon=True)
    collector.start()
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONSTARTUP", None)
    env["PYTHONPATH"] = str(trace_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TMPDIR"] = str(write_root)
    env["EMBEDDED_7Y_TRACE_FD"] = str(trace_write)
    try:
        process = subprocess.Popen(
            [
                str(SANDBOX_EXEC),
                "-f",
                str(sandbox_profile),
                *arguments,
            ],
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(trace_write,),
        )
        stdout, stderr = process.communicate()
        completed = subprocess.CompletedProcess(
            arguments,
            process.returncode,
            stdout,
            stderr,
        )
    finally:
        os.close(trace_write)
        collector.join()
        os.close(trace_read)
    trace_payload = b"".join(trace_chunks).decode("utf-8")
    records = [
        json.loads(line)
        for line in trace_payload.splitlines()
        if line
    ]
    _assert_no_forbidden_access(
        records,
        cwd=cwd,
        forbidden_roots=forbidden_roots,
        write_root=write_root,
    )
    return completed, records


def _assert_completed(
    completed: subprocess.CompletedProcess[str],
    label: str,
) -> None:
    if completed.returncode != 0:
        raise AssertionError(
            f"{label} command failed ({completed.returncode}): "
            f"{completed.stderr.strip()}"
        )
    if completed.stdout != "":
        raise AssertionError(f"{label} command wrote business stdout")


def _assert_result(
    result: dict[str, object],
    request: dict[str, str],
) -> None:
    if tuple(result) != RESULT_FIELDS:
        raise AssertionError("Result fields do not match Contract 1.0 exactly")
    for field in RESULT_FIELDS[:-1]:
        if result[field] != request[field]:
            raise AssertionError(f"Result did not echo Request field: {field}")
    direction = result["predicted_direction"]
    if isinstance(direction, bool) or not isinstance(direction, int):
        raise AssertionError("JSON predicted_direction must be an integer")
    if direction not in {-1, 0, 1}:
        raise AssertionError("predicted_direction must be -1, 0, or 1")


def verify_independence(delivery: Path, fixture_root: Path) -> None:
    """Run one copied delivery pair in a traced blank temporary working root."""
    delivery = Path(delivery).resolve()
    runner_path, metadata_path = _exact_pair(delivery)
    fixture_root = Path(fixture_root)
    fixture_paths: dict[str, Path] = {}
    for filename in PLATFORM_FILENAMES:
        path = fixture_root / filename
        if path.is_symlink() or not path.is_file():
            raise AssertionError(f"missing regular platform fixture: {filename}")
        fixture_paths[filename] = path
    request = _independence_request(fixture_root)
    forbidden_roots = _forbidden_runtime_roots(delivery)

    with tempfile.TemporaryDirectory(prefix="embedded-7y-independence-") as tmp:
        temporary_root = Path(tmp).resolve()
        blank_root = temporary_root / "blank"
        blank_root.mkdir(mode=0o700)
        copied_delivery = blank_root / delivery.name
        copied_delivery.mkdir(mode=0o700)
        copied_runner = copied_delivery / runner_path.name
        copied_metadata = copied_delivery / metadata_path.name
        shutil.copyfile(runner_path, copied_runner)
        shutil.copyfile(metadata_path, copied_metadata)
        copied_runner.chmod(0o600)
        copied_metadata.chmod(0o600)
        copied_data = blank_root / "data"
        copied_data.mkdir(mode=0o700)
        for filename, source in fixture_paths.items():
            destination = copied_data / filename
            shutil.copyfile(source, destination)
            destination.chmod(0o400)

        request_path = copied_data / "request.json"
        request_path.write_text(
            json.dumps(request, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
        request_path.chmod(0o400)
        predict_write_root = temporary_root / "predict-writable"
        prediction_path = predict_write_root / "prediction.json"
        completed, _ = _run_confined_process(
            [
                str(BLACKBOX_PYTHON),
                str(copied_runner),
                "predict",
                "--request",
                str(request_path),
                "--data-dir",
                str(copied_data),
                "--output",
                str(prediction_path),
            ],
            cwd=blank_root,
            write_root=predict_write_root,
            forbidden_roots=forbidden_roots,
        )
        _assert_completed(completed, "independence predict")
        prediction = json.loads(
            prediction_path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
        )
        if not isinstance(prediction, dict):
            raise AssertionError("predict Result must be a JSON object")
        _assert_result(prediction, request)

        requests_path = copied_data / "requests.csv"
        with requests_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=REQUEST_FIELDS)
            writer.writeheader()
            writer.writerow(request)
        requests_path.chmod(0o400)
        backtest_write_root = temporary_root / "backtest-writable"
        backtest_path = backtest_write_root / "backtest.csv"
        completed, _ = _run_confined_process(
            [
                str(BLACKBOX_PYTHON),
                str(copied_runner),
                "backtest",
                "--requests",
                str(requests_path),
                "--data-dir",
                str(copied_data),
                "--output",
                str(backtest_path),
            ],
            cwd=blank_root,
            write_root=backtest_write_root,
            forbidden_roots=forbidden_roots,
        )
        _assert_completed(completed, "independence backtest")
        with backtest_path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != RESULT_FIELDS:
                raise AssertionError(
                    "backtest Result fields do not match Contract 1.0"
                )
            rows = list(reader)
        if len(rows) != 1:
            raise AssertionError("backtest independence run must return one row")
        csv_result: dict[str, object] = dict(rows[0])
        try:
            csv_result["predicted_direction"] = int(
                str(csv_result["predicted_direction"])
            )
        except ValueError as error:
            raise AssertionError("invalid CSV predicted_direction") from error
        _assert_result(csv_result, request)
        if csv_result != prediction:
            raise AssertionError("predict/backtest independence results differ")


def _parse_direction(value: str, label: str) -> int:
    if value not in {"-1", "0", "1"}:
        raise AssertionError(f"{label} must be -1, 0, or 1")
    return int(value)


def _load_audit_phase(
    path: Path,
    *,
    phase: str,
    scheme: FrozenScheme,
) -> list[dict[str, object]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        if not AUDIT_FIELDS.issubset(fields):
            raise AssertionError(
                f"{phase} audit columns missing: {sorted(AUDIT_FIELDS - fields)}"
            )
        source_rows = list(reader)
    selected = [
        row
        for row in source_rows
        if row["candidate_id"] == scheme.candidate_id
    ]
    expected_count = PHASE_COUNTS[phase]
    if len(selected) != expected_count:
        raise AssertionError(
            f"{phase} {scheme.candidate_id}: expected {expected_count} rows, "
            f"found {len(selected)}"
        )
    rows: list[dict[str, object]] = []
    seen_dates: set[str] = set()
    for row in selected:
        if row["phase"] != phase:
            raise AssertionError(f"{phase} audit phase identity mismatch")
        if row["config_hash"] != scheme.candidate_hash:
            raise AssertionError(f"{phase} audit config_hash mismatch")
        feature = _canonical_date(row["feature_date"], f"{phase} feature_date")
        target = _canonical_date(row["target_date"], f"{phase} target_date")
        if feature >= target:
            raise AssertionError(f"{phase} feature_date must precede target_date")
        if row["feature_date"] in seen_dates:
            raise AssertionError(f"{phase} feature_date values must be unique")
        seen_dates.add(row["feature_date"])
        rows.append(
            {
                "phase": phase,
                "feature_date": row["feature_date"],
                "target_date": row["target_date"],
                "label": _parse_direction(row["label"], f"{phase} label"),
                "action": _parse_direction(row["action"], f"{phase} action"),
            }
        )
    rows.sort(key=lambda row: str(row["feature_date"]))
    if len(rows) != len({str(row["feature_date"]) for row in rows}):
        raise AssertionError(f"{phase} feature dates must be unique")
    return rows


def _strict_int_direction(value: str) -> int:
    if value not in {"-1", "0", "1"}:
        raise AssertionError("Result predicted_direction must be -1, 0, or 1")
    return int(value)


def _run_backtest_batch(
    delivery: Path,
    data_dir: Path,
    requests: list[dict[str, str]],
) -> list[dict[str, object]]:
    runner = delivery / f"{delivery.name}.py"
    with tempfile.TemporaryDirectory(prefix="embedded-7y-parity-") as tmp:
        run_root = Path(tmp).resolve()
        requests_path = run_root / "requests.csv"
        write_root = run_root / "writable"
        output_path = write_root / "backtest.csv"
        with requests_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=REQUEST_FIELDS)
            writer.writeheader()
            writer.writerows(requests)
        completed, _ = _run_confined_process(
            [
                str(BLACKBOX_PYTHON),
                str(runner),
                "backtest",
                "--requests",
                str(requests_path),
                "--data-dir",
                str(data_dir),
                "--output",
                str(output_path),
            ],
            cwd=run_root,
            write_root=write_root,
        )
        _assert_completed(completed, "parity")
        with output_path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != RESULT_FIELDS:
                raise AssertionError(
                    "parity Result fields do not match Contract 1.0"
                )
            output_rows = list(reader)
    if len(output_rows) != len(requests):
        raise AssertionError("parity Result cardinality mismatch")
    results: list[dict[str, object]] = []
    for request, result in zip(requests, output_rows, strict=True):
        for field in RESULT_FIELDS[:-1]:
            if result[field] != request[field]:
                raise AssertionError(f"parity Result echo mismatch: {field}")
        results.append(
            {
                **{field: result[field] for field in RESULT_FIELDS[:-1]},
                "predicted_direction": _strict_int_direction(
                    result["predicted_direction"]
                ),
            }
        )
    return results


def _metric(rows: list[dict[str, object]]) -> tuple[float, float]:
    traded = [row for row in rows if row["action"] != 0]
    if not traded:
        raise AssertionError("audited metric has no traded rows")
    correct = sum(row["action"] == row["label"] for row in traded)
    return correct / len(traded), len(traded) / len(rows)


def _assert_metric(name: str, actual: float, expected: float) -> None:
    if abs(actual - expected) > 1e-12:
        display = name.replace("_", " ").upper() if name.startswith("sim") else name
        if name.startswith("sim_"):
            display = f"SIM {name.removeprefix('sim_')}"
        elif name.startswith("real_"):
            display = f"REAL {name.removeprefix('real_')}"
        elif name.startswith("combined_"):
            display = f"combined {name.removeprefix('combined_')}"
        raise AssertionError(
            f"{display} metric mismatch: expected {expected}, found {actual}"
        )


def verify_parity(
    delivery: Path,
    candidate_id: str,
    audit_root: Path,
    data_dir: Path,
    *,
    jobs: int = 1,
) -> dict[str, float]:
    """Replay and compare the exact audited 320-row SIM/REAL vector."""
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise ValueError("jobs must be a positive integer")
    delivery = Path(delivery)
    verify_structure(delivery)
    runner, _ = _exact_pair(delivery)
    if runner.name != f"{delivery.name}.py":
        raise AssertionError("delivery runner name mismatch")
    scheme = _scheme_for_delivery(delivery)
    if candidate_id != scheme.candidate_id:
        raise AssertionError("candidate_id does not match delivery identity")
    audit_root = Path(audit_root)
    data_dir = Path(data_dir)
    phase_rows = {
        phase: _load_audit_phase(
            audit_root / f"{phase.lower()}_predictions.csv",
            phase=phase,
            scheme=scheme,
        )
        for phase in ("SIM", "REAL")
    }
    rows = phase_rows["SIM"] + phase_rows["REAL"]
    if len(rows) != 320:
        raise AssertionError(f"combined audit must contain 320 rows: {len(rows)}")
    calendar, _ = _read_calendar(data_dir)
    requests: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        feature_date = str(row["feature_date"])
        try:
            weekly_cutoff = calendar[feature_date]
        except KeyError as error:
            raise AssertionError(
                f"authoritative calendar lacks feature date: {feature_date}"
            ) from error
        requests.append(
            {
                "request_id": (
                    f"parity-{str(row['phase']).lower()}-{index:03d}"
                ),
                "predict_date": feature_date,
                "feature_date": feature_date,
                "target_date": str(row["target_date"]),
                "daily_cutoff_key": feature_date,
                "weekly_cutoff_key": weekly_cutoff,
                "monthly_cutoff_key": feature_date.replace("-", "")[:6],
            }
        )
    batches = [
        requests[index : index + 100]
        for index in range(0, len(requests), 100)
    ]
    results_by_batch: dict[int, list[dict[str, object]]] = {}
    with ThreadPoolExecutor(max_workers=min(jobs, len(batches))) as executor:
        futures = {
            executor.submit(
                _run_backtest_batch,
                delivery,
                data_dir,
                batch,
            ): index
            for index, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            results_by_batch[futures[future]] = future.result()
    results = [
        result
        for index in range(len(batches))
        for result in results_by_batch[index]
    ]
    if len(results) != len(rows):
        raise AssertionError("combined parity Result cardinality mismatch")
    for row, result in zip(rows, results, strict=True):
        if result["predicted_direction"] != row["action"]:
            raise AssertionError(
                "action mismatch at "
                f"{row['phase']} {row['feature_date']}: "
                f"expected {row['action']}, found "
                f"{result['predicted_direction']}"
            )

    sim_accuracy, sim_trade_rate = _metric(phase_rows["SIM"])
    real_accuracy, real_trade_rate = _metric(phase_rows["REAL"])
    combined_accuracy, combined_trade_rate = _metric(rows)
    metrics = {
        "sim_accuracy": sim_accuracy,
        "sim_trade_rate": sim_trade_rate,
        "real_accuracy": real_accuracy,
        "real_trade_rate": real_trade_rate,
        "combined_accuracy": combined_accuracy,
        "combined_trade_rate": combined_trade_rate,
    }
    for name, expected in FROZEN_METRICS.items():
        _assert_metric(name, metrics[name], expected)
    return metrics
