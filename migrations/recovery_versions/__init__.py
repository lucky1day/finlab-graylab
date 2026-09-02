"""版本级 APPLYING recovery 身份与错误合同。"""

from migrations.recovery_versions.v017 import SPEC as SPEC_017
from migrations.recovery_versions.v018 import SPEC as SPEC_018
from migrations.recovery_versions.v019 import SPEC as SPEC_019
from migrations.recovery_versions.v021 import SPEC as SPEC_021
from migrations.recovery_versions.v022 import SPEC as SPEC_022
from migrations.recovery_versions.v023 import SPEC as SPEC_023


RECOVERY_SPECS = (
    SPEC_017,
    SPEC_018,
    SPEC_019,
    SPEC_021,
    SPEC_022,
    SPEC_023,
)
