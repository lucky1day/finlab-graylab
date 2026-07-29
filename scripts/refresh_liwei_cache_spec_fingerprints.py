"""重算并核对 daily scheduler policy 中的 liwei Phase-A cache 钉值。

## 为什么需要这个工具

`deploy/daily_scheduler_policy_v{1,2}.json` 为每个 liwei cache_group 钉了一个
``cache_spec_fingerprint``。ledger 模式在资格校验层把钉值与运行期实算值逐一比对，
不一致即 fail-closed（不是回退重建）。而 ``_spec_fingerprint`` 的 payload 里掺入了
``python``/``numpy``/``pandas``/``lightgbm`` 版本（``shared.liwei_0616_phase_a_cache``
的 ``_baseline_fingerprint``），因此钉值会随算法配置**或**算法环境依赖的任何变动而
失效。此前钉值靠手工维护，已全部过期。

本工具把钉值改为从**唯一真源**派生：``APPROVED_PHASE_A_CACHE_PUBLISHERS`` 指定的
publisher 方案，按其 ``inference`` 模块的真实取值构造 ``PhaseACacheSpec``，再调用
``_spec_fingerprint``。

## 必须在算法环境运行

钉值必须等于**算法实际运行环境**（``BOND_ALGO_CONDA_ENV``，默认 ``forecast_env``）
算出的值。在服务环境（Python 版本不同）算出的是另一个值，写进 policy 会让 ledger
一样 fail-closed。脚本启动即校验解释器所属环境，不符合直接退出。

## 交叉校验

若本机已存在该 family 的 Phase-A cache current generation，其 manifest 里的
``spec_fingerprint`` 是**运行期真实写下的值**。此时脚本会拿它与本次实算值比对，
不一致说明本脚本的 spec 构造已与方案实现漂移，直接 fail-closed，避免把错值写进 policy。

用法::

    # 只读核对（退出码 0=全部一致，1=存在不一致）
    conda run --no-capture-output -n forecast_env \\
        python scripts/refresh_liwei_cache_spec_fingerprints.py --check

    # 重写 policy 钉值
    conda run --no-capture-output -n forecast_env \\
        python scripts/refresh_liwei_cache_spec_fingerprints.py --write
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.liwei_0616_cache_contract import (  # noqa: E402
    APPROVED_PHASE_A_CACHE_PUBLISHERS,
)
from shared.liwei_0616_phase_a_cache import (  # noqa: E402
    PhaseACacheSpec,
    _spec_fingerprint,
)

# 只维护 v2：ledger 运行时固定加载 v2（``scheduler.daily_runtime`` 导入
# ``POLICY_V2_PATH``），而 v1 是按字节冻结的历史基线（``tests.test_daily_policy_v2``
# 的 ``test_v1_policy_bytes_remain_immutable`` 钉了它的 SHA-256），仍被
# ``harness.daily_real_replay`` 与 ``harness.native_daily_certification`` 当默认使用，
# 不得改写。
POLICY_PATH = PROJECT_ROOT / "deploy" / "daily_scheduler_policy_v2.json"
ADMISSION_PATH = (
    PROJECT_ROOT / "deploy" / "daily_capacity_admission_v2.json"
)
CACHE_ROOT = (
    PROJECT_ROOT / "backtest_artifacts" / "runtime_cache" / "liwei_0616"
)
DEFAULT_ALGO_ENV = "forecast_env"


class FingerprintRefreshError(RuntimeError):
    """钉值重算或核对过程中的 fail-closed 错误。"""


def _require_algo_environment() -> str:
    """确认当前解释器就是算法环境，否则算出的钉值不可用。"""
    expected = os.getenv("BOND_ALGO_CONDA_ENV", DEFAULT_ALGO_ENV).strip()
    actual = Path(sys.prefix).name
    if actual != expected:
        raise FingerprintRefreshError(
            "cache spec fingerprint 必须在算法环境中计算："
            f"expected conda env {expected!r}, running in {actual!r} "
            f"(sys.prefix={sys.prefix}). 请用 "
            f"`conda run --no-capture-output -n {expected} python "
            "scripts/refresh_liwei_cache_spec_fingerprints.py ...` 重跑。"
        )
    return expected


def _resolve_baselines(module: Any) -> tuple[str, ...]:
    """按方案自身声明取 baseline 清单。

    两种既有形态：``required_baselines()``（经 inference 或其 core 暴露）与
    ``PROD_CONFIG["baselines"]``。两者都取不到即 fail-closed，避免静默用错清单。
    """
    getter = getattr(module, "required_baselines", None)
    if callable(getter):
        return tuple(str(name) for name in getter())
    prod_config = getattr(module, "PROD_CONFIG", None)
    if isinstance(prod_config, Mapping) and "baselines" in prod_config:
        return tuple(str(name) for name in prod_config["baselines"])
    raise FingerprintRefreshError(
        f"{module.__name__} 未暴露 required_baselines() 或 PROD_CONFIG['baselines']"
    )


def _resolve_baseline_configs(
    module: Any,
    baselines: Iterable[str],
) -> dict[str, Mapping[str, Any]]:
    """按方案自身声明取每个 baseline 的配置。

    两种既有形态：``BASELINE_CONFIGS[name]`` 与 ``model_config(name)``，
    与各 inference 模块中 ``PhaseACacheSpec(...)`` 的写法一一对应。
    """
    table = getattr(module, "BASELINE_CONFIGS", None)
    if isinstance(table, Mapping):
        return {name: dict(table[name]) for name in baselines}
    model_config = getattr(module, "model_config", None)
    if callable(model_config):
        return {name: model_config(name) for name in baselines}
    raise FingerprintRefreshError(
        f"{module.__name__} 未暴露 BASELINE_CONFIGS 或 model_config()"
    )


def _required_attr(module: Any, name: str) -> Any:
    value = getattr(module, name, None)
    if value is None:
        raise FingerprintRefreshError(f"{module.__name__} 缺少 {name}")
    return value


def _build_spec(family: str, tenor: str, publisher: str) -> PhaseACacheSpec:
    """按 publisher 方案的真实取值构造 spec。"""
    module = importlib.import_module(f"schemes.{publisher}.inference")
    declared_family = _required_attr(module, "CACHE_FAMILY")
    if declared_family != family:
        raise FingerprintRefreshError(
            f"{publisher} 声明的 CACHE_FAMILY={declared_family!r} 与注册表的 "
            f"{family!r} 不一致"
        )
    baselines = _resolve_baselines(module)
    return PhaseACacheSpec(
        cache_family=family,
        tenor=tenor,
        publisher_consumer_id=_required_attr(
            module, "CACHE_PUBLISHER_CONSUMER_ID"
        ),
        baselines=baselines,
        baseline_configs=_resolve_baseline_configs(module, baselines),
        source_ic_screen_start=_required_attr(module, "SOURCE_IC_SCREEN_START"),
        horizon=_required_attr(module, "HORIZON"),
        purge_gap=_required_attr(module, "PURGE_GAP"),
    )


def _runtime_fingerprint(family: str) -> str | None:
    """读该 family current generation manifest 中运行期写下的 spec_fingerprint。"""
    family_dir = CACHE_ROOT / family
    if not family_dir.is_dir():
        return None
    for tenor_dir in sorted(p for p in family_dir.iterdir() if p.is_dir()):
        pointer_path = tenor_dir / "current.json"
        if not pointer_path.is_file():
            continue
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        manifest_path = (
            tenor_dir
            / "generations"
            / str(pointer["generation_id"])
            / "manifest.json"
        )
        if not manifest_path.is_file():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        fingerprint = manifest.get("spec_fingerprint")
        return str(fingerprint) if fingerprint else None
    return None


def compute_fingerprints() -> dict[str, str]:
    """算出每个 cache_group（``family:tenor``）的 spec fingerprint。"""
    computed: dict[str, str] = {}
    for family, (tenor, publisher) in sorted(
        APPROVED_PHASE_A_CACHE_PUBLISHERS.items()
    ):
        fingerprint = _spec_fingerprint(_build_spec(family, tenor, publisher))
        runtime = _runtime_fingerprint(family)
        if runtime is not None and runtime != fingerprint:
            raise FingerprintRefreshError(
                f"{family}: 本脚本实算 {fingerprint} 与运行期 manifest 写下的 "
                f"{runtime} 不一致，说明 spec 构造已与方案实现漂移；"
                "在修正本脚本之前不得写入 policy"
            )
        computed[f"{family}:{tenor}"] = fingerprint
    return computed


def _policy_pins(payload: Mapping[str, Any]) -> dict[str, set[str]]:
    pins: dict[str, set[str]] = {}
    for row in payload.get("schemes") or ():
        fingerprint = row.get("cache_spec_fingerprint")
        if not fingerprint:
            continue
        pins.setdefault(str(row["cache_group"]), set()).add(str(fingerprint))
    return pins


def _rewrite_pins(path: Path, computed: Mapping[str, str]) -> int:
    """就地替换钉值，保留原文件的排版。

    policy 是版本化控制文件，重新序列化会把紧凑的内联数组展开成多行，产生与本次
    变更无关的大片 diff。钉值是内容寻址哈希、每个 cache_group 唯一，因此按
    ``"cache_spec_fingerprint": "<旧值>"`` 的精确字面量做文本替换，既最小化 diff
    也不触碰其它字段。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    replacements: dict[str, str] = {}
    for row in payload.get("schemes") or ():
        current = row.get("cache_spec_fingerprint")
        if not current:
            continue
        group = str(row["cache_group"])
        expected = computed.get(group)
        if expected is None:
            raise FingerprintRefreshError(
                f"policy 中的 cache_group {group!r} 不在 publisher 注册表内"
            )
        existing = replacements.setdefault(str(current), expected)
        if existing != expected:
            raise FingerprintRefreshError(
                f"同一钉值 {current} 被多个 cache_group 使用且期望值不同，"
                "无法安全地按字面量替换"
            )

    text = path.read_text(encoding="utf-8")
    changed = 0
    for old, new in replacements.items():
        if old == new:
            continue
        needle = f'"cache_spec_fingerprint": "{old}"'
        occurrences = text.count(needle)
        if occurrences == 0:
            raise FingerprintRefreshError(
                f"{path.name} 中找不到字面量 {needle}，排版可能与预期不符"
            )
        text = text.replace(needle, f'"cache_spec_fingerprint": "{new}"')
        changed += occurrences

    if changed:
        path.write_text(text, encoding="utf-8")
        # 复核：替换后仍是合法 JSON 且钉值已全部到位。
        verify = json.loads(path.read_text(encoding="utf-8"))
        for row in verify.get("schemes") or ():
            fingerprint = row.get("cache_spec_fingerprint")
            if not fingerprint:
                continue
            expected = computed[str(row["cache_group"])]
            if fingerprint != expected:
                raise FingerprintRefreshError(
                    f"{path.name} 替换后 {row['cache_group']} 钉值仍不正确"
                )
    return changed


def _rebind_capacity_admission() -> bool:
    """把 capacity admission 的 ``policy_sha256`` 重新绑定到当前 policy 字节。

    admission 绑定 policy 的**精确字节**，policy 一变绑定即失效
    （``require_daily_capacity_admission`` 会以 ``policy_sha256 mismatch``
    fail-closed）。因此两者必须同步更新。

    若 admission 已不是 ``BLOCKED``，说明它已带着签名生效；此时改写 policy 会让
    既有签名失配，只能由 operator 重走容量准入流程，脚本在此 fail-closed。
    """
    payload = json.loads(ADMISSION_PATH.read_text(encoding="utf-8"))
    status = payload.get("status")
    if status != "BLOCKED":
        raise FingerprintRefreshError(
            f"capacity admission 当前为 {status!r} 而非 BLOCKED；"
            "改写 policy 会使既有签名失配，请由 operator 重走容量准入流程"
        )
    digest = hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest()
    current = payload.get("policy_sha256")
    if current == digest:
        return False
    text = ADMISSION_PATH.read_text(encoding="utf-8")
    needle = f'"policy_sha256": "{current}"'
    if text.count(needle) != 1:
        raise FingerprintRefreshError(
            f"{ADMISSION_PATH.name} 中 policy_sha256 字面量不唯一，无法安全替换"
        )
    ADMISSION_PATH.write_text(
        text.replace(needle, f'"policy_sha256": "{digest}"'),
        encoding="utf-8",
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "重算 liwei Phase-A cache 钉值并核对/写入 daily scheduler policy。"
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="只读核对；全部一致退出 0，存在不一致退出 1",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="把实算钉值写回 policy 文件",
    )
    args = parser.parse_args()

    environment = _require_algo_environment()
    computed = compute_fingerprints()

    print(f"算法环境: {environment}  (python {sys.version.split()[0]})")
    print(f"cache_group 数: {len(computed)}\n")

    payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    pins = _policy_pins(payload)
    mismatched = 0
    print(f"{POLICY_PATH.name}")
    for group, expected in computed.items():
        actual = pins.get(group)
        if actual is None:
            mismatched += 1
            print(f"  MISSING  {group:<40} 实算={expected[:16]}…")
            continue
        if actual == {expected}:
            print(f"  ok       {group:<40} {expected[:16]}…")
            continue
        mismatched += 1
        print(
            f"  MISMATCH {group:<40} "
            f"policy={sorted(a[:16] for a in actual)} 实算={expected[:16]}…"
        )

    admission_digest = hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest()
    admission = json.loads(ADMISSION_PATH.read_text(encoding="utf-8"))
    admission_bound = admission.get("policy_sha256") == admission_digest
    print(
        f"\n{ADMISSION_PATH.name}\n"
        f"  {'ok      ' if admission_bound else 'MISMATCH'} policy_sha256 "
        f"绑定={'一致' if admission_bound else '过期'}"
    )
    if not admission_bound:
        mismatched += 1

    if args.write:
        changed = _rewrite_pins(POLICY_PATH, computed)
        rebound = _rebind_capacity_admission()
        print(
            f"\n已更新钉值 {changed} 条；"
            f"capacity admission 绑定{'已重算' if rebound else '无需变更'}"
        )
        return 0

    if mismatched:
        print(f"\n存在 {mismatched} 处与实算不一致")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FingerprintRefreshError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
