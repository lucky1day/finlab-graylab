from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shared.exclusive_file_lock import (
    ExclusiveFileLock,
    ExclusiveFileLockUnavailable,
)


class ExclusiveFileLockTests(unittest.TestCase):
    def test_second_holder_is_rejected_until_first_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory).resolve() / "singleton.lock"
            first = ExclusiveFileLock(path)
            second = ExclusiveFileLock(path)

            first.acquire()
            try:
                with self.assertRaises(ExclusiveFileLockUnavailable):
                    second.acquire()
                self.assertTrue(first.acquired)
                self.assertFalse(second.acquired)
            finally:
                first.release()

            second.acquire()
            self.assertTrue(second.acquired)
            second.release()


if __name__ == "__main__":
    unittest.main()
