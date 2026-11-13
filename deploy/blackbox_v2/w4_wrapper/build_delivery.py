"""生成 W4 二进制算法的 Blackbox V2 封装交付（`{scheme_id}.py` + `{scheme_id}.json`）。

确定性：同一源码包与参数，生成字节完全相同的两个文件。源码包里含明文口令的 `db_config.py` 不进入交付，
换成不含任何凭据的占位模块；其余文件（含全部 `.so`）原样打包。生成后立即用平台自己的静态策略与 Metadata
合同校验，任何一项不通过都不写出文件。
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import io
import json
import lzma
import os
import sys
import tarfile
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from shared.blackbox_v2._script_policy import validate_delivery_script  # noqa: E402
from shared.blackbox_v2.contracts import load_metadata  # noqa: E402

TEMPLATE_PATH = Path(__file__).with_name("shell_template.py")
CREDENTIAL_FREE_DB_CONFIG = (
    '"""Credentials were removed by the wrapper builder; the wrapped process never connects to a database."""\n'
    "DB_CONFIG: dict = {}\n"
)
_EXCLUDED_NAMES = {"__pycache__", ".DS_Store"}
_PLACEHOLDERS = ("SCHEME_ID", "FREQUENCY", "PACKAGE_SHA256", "SOURCE_PACKAGE_IDENTITY", "PAYLOAD_B64")


def pack_source_package(source_package: Path) -> bytes:
    """确定性 tar：排序、uid/gid/mtime 归零、模式归一；排除缓存与含口令的 `db_config.py`。"""
    source_package = Path(source_package)
    if not (source_package / "daily_project" / "src").is_dir():
        raise ValueError("source package must be the forecast_project directory")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for directory, directories, files in os.walk(source_package):
            directories[:] = sorted(name for name in directories if name not in _EXCLUDED_NAMES)
            relative = Path(directory).relative_to(source_package)
            if relative != Path("."):
                _add(archive, Path("forecast_project") / relative, None, is_dir=True)
            for name in sorted(files):
                if name in _EXCLUDED_NAMES or name.endswith(".pyc"):
                    continue
                path = Path(directory) / name
                arcname = Path("forecast_project") / relative / name
                if arcname == Path("forecast_project/db_config.py"):
                    _add(archive, arcname, CREDENTIAL_FREE_DB_CONFIG.encode("utf-8"), executable=False)
                else:
                    _add(archive, arcname, path.read_bytes(), executable=bool(path.stat().st_mode & 0o111))
    return buffer.getvalue()


def _add(archive: tarfile.TarFile, arcname: Path, payload: bytes | None, *, is_dir: bool = False, executable: bool = False) -> None:
    info = tarfile.TarInfo(arcname.as_posix())
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    if is_dir:
        info.type, info.mode = tarfile.DIRTYPE, 0o755
        archive.addfile(info)
        return
    info.size = len(payload)
    info.mode = 0o755 if executable else 0o644
    archive.addfile(info, io.BytesIO(payload))


def render_script(*, tar_bytes: bytes, scheme_id: str, frequency: str, source_identity: str) -> str:
    compressed = lzma.compress(tar_bytes, format=lzma.FORMAT_XZ, preset=9 | lzma.PRESET_EXTREME, check=lzma.CHECK_SHA256)
    encoded = base64.b64encode(compressed).decode("ascii")
    lines = "\n".join(encoded[index:index + 100] for index in range(0, len(encoded), 100))
    values = {
        "SCHEME_ID": scheme_id,
        "FREQUENCY": frequency,
        "PACKAGE_SHA256": hashlib.sha256(tar_bytes).hexdigest(),
        "SOURCE_PACKAGE_IDENTITY": source_identity,
        "PAYLOAD_B64": "\n" + lines + "\n",
    }
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    for key in _PLACEHOLDERS:
        marker = f"__{key}__"
        if text.count(marker) != 1:
            raise ValueError(f"template placeholder {marker} must appear exactly once")
        text = text.replace(marker, values[key])
    return text


def build_delivery(
    *,
    source_package: Path,
    scheme_id: str,
    frequency: str,
    source_identity: str,
    metadata: dict,
    output_dir: Path,
) -> tuple[Path, Path]:
    script_text = render_script(
        tar_bytes=pack_source_package(source_package), scheme_id=scheme_id, frequency=frequency, source_identity=source_identity
    )
    ast.parse(script_text)
    metadata_text = json.dumps({**metadata, "scheme_id": scheme_id}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.TemporaryDirectory() as scratch:  # 先在临时目录校验，通过后才写出
        script_path, metadata_path = Path(scratch) / f"{scheme_id}.py", Path(scratch) / f"{scheme_id}.json"
        script_path.write_text(script_text, encoding="utf-8", newline="")
        metadata_path.write_text(metadata_text, encoding="utf-8", newline="")
        validate_delivery_script(script_path)
        loaded = load_metadata(metadata_path)
        if loaded.input_profile != "raw_wind_v1":
            raise ValueError("wrapped binary deliveries must declare input_profile raw_wind_v1")
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        targets = (output_dir / script_path.name, output_dir / metadata_path.name)
        targets[0].write_text(script_text, encoding="utf-8", newline="")
        targets[1].write_text(metadata_text, encoding="utf-8", newline="")
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-package", type=Path, required=True)
    parser.add_argument("--scheme-id", required=True)
    parser.add_argument("--frequency", required=True, choices=["D1Y", "D5Y", "D10Y"])
    parser.add_argument("--source-identity", required=True, help="源码包冻结身份（树哈希），仅作审计常量")
    parser.add_argument("--metadata", type=Path, required=True, help="Metadata JSON 模板（不含 scheme_id 也可）")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    script, document = build_delivery(
        source_package=args.source_package, scheme_id=args.scheme_id, frequency=args.frequency,
        source_identity=args.source_identity, metadata=metadata, output_dir=args.output_dir,
    )
    print(f"{script} ({script.stat().st_size} bytes)\n{document}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
