"""显式 no-persist 的 14 个冻结 Native 日频真实认证入口。"""

import sys
from pathlib import Path


sys.dont_write_bytecode = True
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from harness.native_daily_certification import main


if __name__ == "__main__":
    raise SystemExit(main())
