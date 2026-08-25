"""跨周首日 DataBridge 自锁的回归测试。

生产故障形态：已发布 current 的最高周键停在上周，而 source 已进入新周。
刷新入口在构建前用旧 current 解析 continuity authority，于是出现
"必须先有新周数据才允许生成新周数据"的自锁。

本文件只保留生产 `refresh_current` 入口的完整闭环：旧 current 只到上周，
真实 authority 解析后，新候选必须发布出本周数据。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.data_bridge.refresh import (  # noqa: E402
    DataBridgeRefreshConfig,
    DownloadRound,
    run_full_refresh,
)
from shared.data_bridge.validation import (  # noqa: E402
    validate_dataset,
    write_validated_dataset,
)

SCHEMA_PATH = PROJECT_ROOT / "shared" / "blackbox_v2" / "data_bridge_v1_schema.json"
LAST_WEEK = "202629"
NEW_WEEK = "202630"
MONTH = "202607"


def _schema_columns() -> dict[str, list[str]]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return {
        filename: list(spec["columns"])
        for filename, spec in schema["files"].items()
    }


def _frame(filename: str, keys: list[str]) -> pd.DataFrame:
    """按 schema baseline 列构造确定性的合法数据帧。"""
    columns = _schema_columns()[filename]
    rows = []
    for row_index, key in enumerate(keys):
        row: dict[str, object] = {columns[0]: key}
        for column_index, column in enumerate(columns[1:]):
            row[column] = float(row_index + column_index + 1)
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


DAILY_START = "2026-07-06"


def _daily_keys(end_date: str) -> list[str]:
    """日频只追加不回撤，历史键必须跨代保留。"""
    first = date.fromisoformat(DAILY_START)
    last = date.fromisoformat(end_date)
    span = (last - first).days
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range(span + 1)
    ]


class _FakeRoundBuilder:
    """用固定周期键集合构造候选目录，走真实 validate_dataset。"""

    def __init__(
        self,
        *,
        staging_root: Path,
        daily_end: str,
        week_ids: list[str],
        month_ids: list[str],
    ) -> None:
        self.staging_root = staging_root
        self.daily_end = daily_end
        self.week_ids = week_ids
        self.month_ids = month_ids
        self.calls = 0

    def build(
        self,
        round_id: str,
        *,
        end_date: str,
        expected_daily_date: str,
        previous_keys=None,
        continuity_cutoffs=None,
    ) -> DownloadRound:
        self.calls += 1
        frames = {
            "daily_output.csv": _frame(
                "daily_output.csv",
                _daily_keys(self.daily_end),
            ),
            "weekly_output.csv": _frame("weekly_output.csv", self.week_ids),
            "monthly_output.csv": _frame("monthly_output.csv", self.month_ids),
        }
        dataset = validate_dataset(
            frames,
            schema_path=SCHEMA_PATH,
            expected_daily_date=expected_daily_date,
            previous_keys=previous_keys,
            continuity_cutoffs=continuity_cutoffs,
        )
        directory = self.staging_root / f"{round_id}-{self.calls}"
        write_validated_dataset(dataset, directory)
        return DownloadRound(
            round_id=round_id,
            directory=directory,
            dataset=dataset,
            digest=dataset.business_digest,
        )


class _LaunchdShapedBuilder(_FakeRoundBuilder):
    """冒充受控 MySQL exporter 的构造签名，用于驱动生产入口。"""

    staging_root_holder: Path
    daily_end_holder: str
    week_ids_holder: list[str]

    def __init__(self, *, engine, config) -> None:  # noqa: ARG002
        super().__init__(
            staging_root=type(self).staging_root_holder,
            daily_end=type(self).daily_end_holder,
            week_ids=type(self).week_ids_holder,
            month_ids=[MONTH],
        )


class _CrossWeekFixture(unittest.TestCase):
    """跨周场景的共享夹具：真实 store / publish / 校验链路。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="bfl-crossweek-")
        root = Path(self._tmp.name)
        self.data_root = root / "data"
        self.runtime_root = root / "runtime"
        self.staging = root / "staging"
        for path in (self.data_root, self.runtime_root, self.staging):
            path.mkdir(parents=True)
            path.chmod(0o700)
        self.config = DataBridgeRefreshConfig(
            data_root=self.data_root,
            runtime_root=self.runtime_root,
            schema_path=SCHEMA_PATH,
        )
        self._publish_index = 0
        self.addCleanup(self._tmp.cleanup)

    def _publish(
        self,
        *,
        daily_end: str,
        week_ids: list[str],
        refresh_date: str,
        continuity_authority=None,
    ):
        self._publish_index += 1
        builder = _FakeRoundBuilder(
            staging_root=self.staging / f"publish-{self._publish_index}",
            daily_end=daily_end,
            week_ids=week_ids,
            month_ids=[MONTH],
        )
        return run_full_refresh(
            config=self.config,
            expected_daily_date=daily_end,
            refresh_date=refresh_date,
            publish=True,
            continuity_authority=continuity_authority,
            round_builder=builder,
        )

    def _publish_last_week_generation(self):
        return self._publish(
            daily_end="2026-07-17",
            week_ids=[LAST_WEEK],
            refresh_date="2026-07-18",
        )

    @contextmanager
    def _as_of_source(self):
        """让真实解析链路跑起来，只替换 DB 读取与索引构建。"""
        weekly_index = pd.DataFrame(
            [
                {"week_id": LAST_WEEK, "available_date": "2026-07-13"},
                {"week_id": NEW_WEEK, "available_date": "2026-07-20"},
            ]
        )
        monthly_index = pd.DataFrame(
            [{"month_id": MONTH, "available_date": "2026-07-01"}]
        )
        metadata = pd.DataFrame({"indicators_code": ["X0000001"]})
        with (
            mock.patch(
                "shared.data_service.read_factor_metadata_from_db",
                return_value=metadata,
            ),
            mock.patch(
                "shared.data_service.select_monthly_factor_metadata",
                return_value=metadata,
            ),
            mock.patch(
                "shared.data_service.read_weekly_long_from_db",
                return_value=pd.DataFrame(),
            ),
            mock.patch(
                "shared.data_service.read_monthly_long_from_db",
                return_value=pd.DataFrame(),
            ),
            mock.patch(
                "shared.data_service.build_weekly_cutoff_index_from_frames",
                return_value=weekly_index,
            ),
            mock.patch(
                "shared.data_service.build_monthly_cutoff_index_from_frames",
                return_value=monthly_index,
            ),
        ):
            yield

    @staticmethod
    def _engine():
        connection = mock.MagicMock()
        connection.exec_driver_sql.return_value = None
        engine = mock.MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        engine.connect.return_value.__exit__.return_value = False
        return engine

class ProductionEntryCrossWeekTests(_CrossWeekFixture):
    """驱动 launchd 生产入口 refresh_current 的跨周回归。

    只使用两个版本都存在的公共签名，因此可以直接在修复前的代码上运行，
    用来证明该回归确实复现了生产自锁，而不是只反映 API 变化。
    """

    def _run_production_entry(self, *, daily_end: str, week_ids: list[str]):
        import scripts.refresh_data_bridge_current as entry

        _LaunchdShapedBuilder.staging_root_holder = (
            self.staging / f"entry-{daily_end}"
        )
        _LaunchdShapedBuilder.daily_end_holder = daily_end
        _LaunchdShapedBuilder.week_ids_holder = week_ids
        with (
            self._as_of_source(),
            mock.patch.dict(
                "os.environ",
                {entry.LAUNCHD_PUBLISHER_ENV: entry.LAUNCHD_PUBLISHER_VALUE},
            ),
            mock.patch.object(
                entry,
                "create_sqlalchemy_engine",
                return_value=self._engine(),
            ),
            mock.patch.object(
                entry,
                "MySqlDataBridgeRoundBuilder",
                _LaunchdShapedBuilder,
            ),
            mock.patch(
                "shared.data_bridge.mysql_exporter.MySqlDataBridgeRoundBuilder",
                _LaunchdShapedBuilder,
            ),
        ):
            return entry.refresh_current(
                refresh_date="2026-07-21",
                expected_feature_date=daily_end,
                publish=True,
                config=self.config,
            )

    def test_production_entry_crosses_week_and_publishes_new_week(self) -> None:
        """周一跨周首刷：旧 current 只到上周，入口必须能发布出新周。"""
        self._publish_last_week_generation()
        result = self._run_production_entry(
            daily_end="2026-07-20",
            week_ids=[LAST_WEEK, NEW_WEEK],
        )
        self.assertTrue(result.published)
        self.assertEqual(
            result.state["files"]["weekly_output.csv"]["max_key"],
            NEW_WEEK,
        )
        published = pd.read_csv(self.data_root / "current" / "weekly_output.csv")
        self.assertIn(
            int(NEW_WEEK),
            published["week_id"].astype(int).tolist(),
        )



if __name__ == "__main__":
    unittest.main()
