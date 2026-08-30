"""版本级 APPLYING recovery 身份与错误合同。"""

from migrations.recovery_versions.v017 import SPEC as SPEC_017
from migrations.recovery_versions.v018 import SPEC as SPEC_018
from migrations.recovery_versions.v019 import SPEC as SPEC_019
from migrations.recovery_versions.v021 import SPEC as SPEC_021


RECOVERY_SPECS = (SPEC_017, SPEC_018, SPEC_019, SPEC_021)
