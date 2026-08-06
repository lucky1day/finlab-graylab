"""仍在生产输入契约中使用的 0629 source-backed 输入身份。"""

from __future__ import annotations


APPROVED_0629_LIVE_SOURCE_SCHEMES = frozenset(
    {
        "daily_10y_lgbm_10y04_0629",
        "daily_1y_xgb_1y13_0629",
        "daily_5y_lgbm_5y10_0629",
    }
)


# 这是 source-backed Native 输入的 immutable package fence，不是自然调度
# policy。过去它被错误地附着在 daily ledger policy 中；现在由实际仍在使用
# 它的 signal-gap/live adapter 共同引用。
_APPROVED_0629_SOURCE_PACKAGE_SHA256 = (
    "de63375f51810962ad10162444f93f7b2fbde6236b7f4b9921734ae6b8fad1e3"
)

APPROVED_0629_LIVE_SOURCE_PACKAGE_SHA256_BY_SCHEME = {
    scheme_id: _APPROVED_0629_SOURCE_PACKAGE_SHA256
    for scheme_id in APPROVED_0629_LIVE_SOURCE_SCHEMES
}
