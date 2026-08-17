"""一次性生产调度控制面的规范身份。"""

LAUNCHD_ONE_SHOT_CONTROL_PLANE = "launchd_one_shot"
SYSTEMD_ONE_SHOT_CONTROL_PLANE = "systemd_one_shot"
SCHEDULED_ONE_SHOT_CONTROL_PLANES = frozenset(
    {
        LAUNCHD_ONE_SHOT_CONTROL_PLANE,
        SYSTEMD_ONE_SHOT_CONTROL_PLANE,
    }
)

DATABRIDGE_LAUNCHD_PRODUCER = "launchd-one-shot"
DATABRIDGE_SYSTEMD_PRODUCER = "systemd-one-shot"
DATABRIDGE_ONE_SHOT_PRODUCERS = frozenset(
    {
        DATABRIDGE_LAUNCHD_PRODUCER,
        DATABRIDGE_SYSTEMD_PRODUCER,
    }
)


def require_scheduled_one_shot_control_plane(value: object) -> str:
    """返回规范的一次性控制面身份，未知值一律拒绝。"""
    normalized = str(value or "").strip()
    if normalized not in SCHEDULED_ONE_SHOT_CONTROL_PLANES:
        raise ValueError("unsupported scheduled one-shot control plane")
    return normalized
