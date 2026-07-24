"""兼容导出：配置 schema 实现位于不依赖 harness 的 shared 低层。"""

from shared.scheme_config_schema import (
    ALLOWED_FREQUENCIES,
    ALLOWED_INPUT_SOURCES,
    ALLOWED_RUNTIME_TYPES,
    ALLOWED_STATUS,
    ALLOWED_TASK_TYPES,
    ALLOWED_TENORS,
    ALLOWED_VERSION_STATUS,
    SCHEME_ID_PATTERN,
    TASK_TYPE_ERROR,
    validate_config,
)

__all__ = [
    "ALLOWED_FREQUENCIES",
    "ALLOWED_INPUT_SOURCES",
    "ALLOWED_RUNTIME_TYPES",
    "ALLOWED_STATUS",
    "ALLOWED_TASK_TYPES",
    "ALLOWED_TENORS",
    "ALLOWED_VERSION_STATUS",
    "SCHEME_ID_PATTERN",
    "TASK_TYPE_ERROR",
    "validate_config",
]
