"""经过审阅的旧脚本校验策略摘要与等价目标。"""

from types import MappingProxyType


EQUIVALENT_SCRIPT_POLICY_DIGESTS = MappingProxyType({
    # 仅覆盖从 intake.py 原样搬移静态校验的那次变更。
    "ada0638afefdfeb64bbf681f423b68f59134b0f40d0ab7d5713411252ee77c3c":
        "0d49ca3996a4f64048b0cc846b853c0e0a925b046751ed9f59f9e632fd9e9e52",
})
