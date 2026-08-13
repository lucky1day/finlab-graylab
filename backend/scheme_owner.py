"""方案归属同事的只读登记。

归属是平台对方案的登记信息，按 registry composite ``scheme_id`` 索引。它不
参与任何算法计算或 join，也不进入 ``scheme_version``；它由 StaticGate 做精确
read-back，并由 Dashboard 作为展示字段读取，因此记录在版本控制文件中，而不是
方案配置或数据库。
"""

from shared.scheme_owner_registry import (
    OWNER_FILE_RELATIVE_PATH,
    OWNER_SCHEMA_VERSION,
    SchemeOwnerError,
    load_scheme_owners,
)


__all__ = [
    "OWNER_FILE_RELATIVE_PATH",
    "OWNER_SCHEMA_VERSION",
    "SchemeOwnerError",
    "load_scheme_owners",
]
