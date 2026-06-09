import unittest

from backend import services


class MetricsCompareRemovedTests(unittest.TestCase):
    def test_metrics_compare_service_is_removed(self) -> None:
        """候选方案排行已覆盖该视角，service 不再保留横向对比聚合。"""
        self.assertFalse(hasattr(services, "metrics_compare"))


if __name__ == "__main__":
    unittest.main()
