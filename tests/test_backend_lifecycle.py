import unittest

from backend import services


class SchemesLifecycleRemovedTests(unittest.TestCase):
    def test_schemes_lifecycle_service_is_removed(self) -> None:
        """方案生命周期健康概览下线后，service 不再保留该聚合入口。"""
        self.assertFalse(hasattr(services, "schemes_lifecycle"))


if __name__ == "__main__":
    unittest.main()
