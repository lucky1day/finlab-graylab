from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ROOT = PROJECT_ROOT / "deploy" / "blackbox_v2"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the frozen Blackbox V2 runtime environment.")
    parser.add_argument("--profile", type=Path, default=DEPLOY_ROOT / "runtime_profile_v1.json")
    parser.add_argument("--manifest", type=Path, default=DEPLOY_ROOT / "environment_manifest.json")
    args = parser.parse_args()

    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    errors: list[str] = []
    env_name = str(profile["conda_env"])
    if manifest.get("conda_env") != env_name:
        errors.append("profile and environment manifest conda_env values differ")
    if manifest.get("runtime_profile") != profile.get("profile_name"):
        errors.append("profile and environment manifest runtime_profile values differ")

    actual_raw = json.loads(
        subprocess.run(
            ["conda", "list", "-n", env_name, "--json"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    actual_packages = _normalize_packages(actual_raw)
    actual_fingerprint = _fingerprint(actual_packages)
    if actual_fingerprint != manifest.get("environment_fingerprint"):
        errors.append(
            "environment package fingerprint changed: "
            f"expected={manifest.get('environment_fingerprint')}, actual={actual_fingerprint}"
        )
    if actual_packages != manifest.get("explicit_packages"):
        errors.append("environment package list changed")
    if profile.get("sandbox_enabled") and shutil.which("sandbox-exec") is None:
        errors.append("sandbox-exec is required but unavailable")

    import_probe = subprocess.run(
        [
            "conda",
            "run",
            "--no-capture-output",
            "-n",
            env_name,
            "python",
            "-c",
            "import numpy,pandas,sklearn,lightgbm,xgboost",
        ],
        capture_output=True,
        text=True,
    )
    if import_probe.returncode != 0:
        errors.append(f"required package import probe failed: {import_probe.stderr.strip()}")

    result = {
        "ok": not errors,
        "runtime_profile": profile.get("profile_name"),
        "conda_env": env_name,
        "environment_fingerprint": actual_fingerprint,
        "package_count": len(actual_packages),
        "sandbox_exec": shutil.which("sandbox-exec"),
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


def _normalize_packages(raw: list[dict]) -> list[dict]:
    keys = ("name", "version", "build_string", "channel", "platform")
    packages = [{key: item.get(key) for key in keys} for item in raw]
    return sorted(packages, key=lambda item: (item["name"], item["version"], item["build_string"]))


def _fingerprint(packages: list[dict]) -> str:
    canonical = json.dumps(packages, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
