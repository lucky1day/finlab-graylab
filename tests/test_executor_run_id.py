from __future__ import annotations

import os
import unittest
import inspect
import signal
import stat
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class _ExplicitLegacyModeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._mode_patcher = patch.dict(
            os.environ,
            {"BOND_DAILY_COORDINATOR_MODE": "legacy"},
        )
        self._mode_patcher.start()

    def tearDown(self) -> None:
        self._mode_patcher.stop()


class ExecutorRunIdTests(_ExplicitLegacyModeTestCase):
    def test_algorithm_environment_propagates_daily_coordinator_mode(
        self,
    ) -> None:
        from scheduler.executor import _build_algorithm_environment

        with patch.dict(
            os.environ,
            {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
            clear=False,
        ):
            environment = _build_algorithm_environment()

        self.assertEqual(
            environment["BOND_DAILY_COORDINATOR_MODE"],
            "ledger",
        )

    def test_algorithm_environment_rejects_inherited_pycache_prefix(
        self,
    ) -> None:
        from scheduler.executor import _build_algorithm_environment

        with patch.dict(
            os.environ,
            {
                "PYTHONPYCACHEPREFIX": "/private/empty-cert-pycache",
                "PYTHONDONTWRITEBYTECODE": "0",
                "CONDA_EXE": "/private/untrusted-conda",
                "CONDA_PYTHON_EXE": "/private/untrusted-python",
                "_CE_CONDA": "bogus",
                "_CE_M": "bogus",
            },
            clear=False,
        ):
            environment = _build_algorithm_environment()

        self.assertNotIn("PYTHONPYCACHEPREFIX", environment)
        for name in (
            "CONDA_EXE",
            "CONDA_PYTHON_EXE",
            "_CE_CONDA",
            "_CE_M",
        ):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")

    def test_native_subprocess_uses_fresh_private_pycache_prefix(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess

        captured: dict[str, object] = {}

        def run_process(_cmd, **kwargs):
            environment = kwargs["env"]
            pycache = Path(environment["PYTHONPYCACHEPREFIX"])
            captured["path"] = pycache
            captured["mode"] = stat.S_IMODE(pycache.stat().st_mode)
            captured["empty"] = not any(pycache.iterdir())
            return SimpleNamespace(stdout="[]")

        with (
            patch.dict(
                os.environ,
                {
                    "PYTHONPYCACHEPREFIX":
                        "/private/untrusted-existing-pycache",
                },
                clear=False,
            ),
            patch(
                "scheduler.executor._run_process_group",
                side_effect=run_process,
            ),
        ):
            self.assertEqual(
                run_scheme_subprocess(
                    "demo",
                    "2026-07-03",
                    algo_env="test_env",
                    timeout_sec=7,
                ),
                [],
            )

        self.assertEqual(captured["mode"], 0o700)
        self.assertTrue(captured["empty"])
        self.assertFalse(Path(captured["path"]).exists())

    def test_non_source_scheme_rejects_explicit_source_database_config(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess

        with self.assertRaisesRegex(
            ValueError,
            "only valid for source schemes",
        ):
            run_scheme_subprocess(
                "demo",
                "2026-07-03",
                source_database_config=SimpleNamespace(),
            )

    def test_native_process_started_callback_runs_once_before_communicate(
        self,
    ) -> None:
        from scheduler.executor import _run_process_group

        events: list[object] = []

        class RecordingGuard:
            def __enter__(self):
                events.append("guard-enter")
                return self

            def __exit__(self, *_exc_info):
                events.append("guard-exit")

        class FakeProcess:
            pid = 12345
            returncode = 0
            stdout = None
            stderr = None

            def communicate(self, timeout=None):
                events.append(("communicate", timeout))
                return "[]", ""

        def process_started(pid: int, pgid: int) -> None:
            events.append(("started", pid, pgid))

        def process_fence() -> None:
            events.append("epoch-fence")

        def popen(*_args, **_kwargs):
            events.append("popen")
            return FakeProcess()

        def getpgid(pid: int) -> int:
            events.append(("capture-pgid", pid))
            return 67890

        with (
            patch(
                "scheduler.executor.subprocess.Popen",
                side_effect=popen,
            ),
            patch("scheduler.executor.os.getpgid", side_effect=getpgid),
        ):
            completed = _run_process_group(
                ["demo"],
                cwd=Path.cwd(),
                env={},
                timeout=7,
                process_started=process_started,
                process_fence=process_fence,
                process_start_guard=RecordingGuard(),
            )

        self.assertEqual(completed.stdout, "[]")
        self.assertEqual(
            events,
            [
                "guard-enter",
                "epoch-fence",
                "popen",
                ("capture-pgid", 12345),
                ("started", 12345, 67890),
                "epoch-fence",
                "guard-exit",
                ("communicate", 7),
            ],
        )

    def test_native_post_popen_epoch_drift_kills_process_group(
        self,
    ) -> None:
        from scheduler.executor import _run_process_group

        events: list[object] = []

        class EpochDrift(RuntimeError):
            pass

        class FakeProcess:
            pid = 12345
            returncode = None
            stdout = None
            stderr = None

            def communicate(self, timeout=None):
                raise AssertionError(
                    "epoch-drifted process must not communicate"
                )

        fence_calls = 0

        def process_fence() -> None:
            nonlocal fence_calls
            fence_calls += 1
            events.append(("epoch-fence", fence_calls))
            if fence_calls == 2:
                raise EpochDrift("epoch changed after Popen")

        class RecordingGuard:
            def __enter__(self):
                events.append("guard-enter")
                return self

            def __exit__(self, *_exc_info):
                events.append("guard-exit")

        def process_started(pid: int, pgid: int) -> None:
            events.append(("started", pid, pgid))

        def killpg(pgid: int, signum: int) -> None:
            events.append(("killpg", pgid, signum))

        with (
            patch(
                "scheduler.executor.subprocess.Popen",
                return_value=FakeProcess(),
            ),
            patch("scheduler.executor.os.getpgid", return_value=67890),
            patch("scheduler.executor.os.killpg", side_effect=killpg),
            patch(
                "scheduler.process_control."
                "_wait_for_process_group_exit",
                return_value=True,
            ),
        ):
            with self.assertRaisesRegex(EpochDrift, "after Popen"):
                _run_process_group(
                    ["demo"],
                    cwd=Path.cwd(),
                    env={},
                    timeout=7,
                    process_started=process_started,
                    process_fence=process_fence,
                    process_start_guard=RecordingGuard(),
                )

        self.assertEqual(
            events,
            [
                "guard-enter",
                ("epoch-fence", 1),
                ("started", 12345, 67890),
                ("epoch-fence", 2),
                ("killpg", 67890, signal.SIGTERM),
                "guard-exit",
            ],
        )

    def test_native_popen_failure_releases_process_start_guard(
        self,
    ) -> None:
        from scheduler.executor import _run_process_group

        events: list[str] = []

        class RecordingGuard:
            def __enter__(self):
                events.append("guard-enter")
                return self

            def __exit__(self, *_exc_info):
                events.append("guard-exit")

        def process_fence() -> None:
            events.append("epoch-fence")

        def popen(*_args, **_kwargs):
            events.append("popen")
            raise OSError("fork failed")

        with patch(
            "scheduler.executor.subprocess.Popen",
            side_effect=popen,
        ):
            with self.assertRaisesRegex(OSError, "fork failed"):
                _run_process_group(
                    ["demo"],
                    cwd=Path.cwd(),
                    env={},
                    timeout=7,
                    process_fence=process_fence,
                    process_start_guard=RecordingGuard(),
                )

        self.assertEqual(
            events,
            [
                "guard-enter",
                "epoch-fence",
                "popen",
                "guard-exit",
            ],
        )

    def test_native_guard_exit_failure_kills_registered_process_group(
        self,
    ) -> None:
        from scheduler.executor import _run_process_group

        events: list[object] = []

        class GuardReleaseError(RuntimeError):
            pass

        class FailingExitGuard:
            def __enter__(self):
                events.append("guard-enter")
                return self

            def __exit__(self, *_exc_info):
                events.append("guard-exit")
                raise GuardReleaseError("guard release failed")

        class FakeProcess:
            pid = 12345
            returncode = None
            stdout = None
            stderr = None

            def communicate(self, timeout=None):
                raise AssertionError(
                    "guard-release failure must stop algorithm runtime"
                )

        def process_started(pid: int, pgid: int) -> None:
            events.append(("started", pid, pgid))

        def killpg(pgid: int, signum: int) -> None:
            events.append(("killpg", pgid, signum))

        with (
            patch(
                "scheduler.executor.subprocess.Popen",
                return_value=FakeProcess(),
            ),
            patch(
                "scheduler.executor.os.getpgid",
                return_value=67890,
            ),
            patch(
                "scheduler.executor.os.killpg",
                side_effect=killpg,
            ),
            patch(
                "scheduler.process_control."
                "_wait_for_process_group_exit",
                return_value=True,
            ),
        ):
            with self.assertRaisesRegex(
                GuardReleaseError,
                "guard release failed",
            ):
                _run_process_group(
                    ["demo"],
                    cwd=Path.cwd(),
                    env={},
                    timeout=7,
                    process_started=process_started,
                    process_start_guard=FailingExitGuard(),
                )

        self.assertEqual(
            events,
            [
                "guard-enter",
                ("started", 12345, 67890),
                "guard-exit",
                ("killpg", 67890, signal.SIGTERM),
            ],
        )

    def test_native_guard_exit_failure_fences_unconfirmed_cleanup(
        self,
    ) -> None:
        from scheduler.executor import _run_process_group
        from scheduler.process_control import (
            ProcessRegistrationCleanupError,
        )

        events: list[object] = []

        class GuardReleaseError(RuntimeError):
            pass

        class FailingExitGuard:
            def __enter__(self):
                return self

            def __exit__(self, *_exc_info):
                raise GuardReleaseError("guard release failed")

        class FakeProcess:
            pid = 12345
            returncode = None
            stdout = None
            stderr = None

            def communicate(self, timeout=None):
                raise AssertionError(
                    "unconfirmed cleanup must stop algorithm runtime"
                )

        def killpg(pgid: int, signum: int) -> None:
            events.append(("killpg", pgid, signum))

        with (
            patch(
                "scheduler.executor.subprocess.Popen",
                return_value=FakeProcess(),
            ),
            patch(
                "scheduler.executor.os.getpgid",
                return_value=67890,
            ),
            patch(
                "scheduler.executor.os.killpg",
                side_effect=killpg,
            ),
            patch(
                "scheduler.process_control."
                "_wait_for_process_group_exit",
                side_effect=(False, False),
            ),
        ):
            with self.assertRaises(
                ProcessRegistrationCleanupError
            ) as raised:
                _run_process_group(
                    ["demo"],
                    cwd=Path.cwd(),
                    env={},
                    timeout=7,
                    process_start_guard=FailingExitGuard(),
                )

        self.assertIsInstance(
            raised.exception.registration_error,
            GuardReleaseError,
        )
        self.assertFalse(
            raised.exception.termination.confirmed_gone
        )
        self.assertEqual(
            events,
            [
                ("killpg", 67890, signal.SIGTERM),
                ("killpg", 67890, signal.SIGKILL),
            ],
        )

    def test_native_unconfirmed_registration_cleanup_wins_over_guard_exit(
        self,
    ) -> None:
        from scheduler.executor import _run_process_group
        from scheduler.process_control import (
            ProcessRegistrationCleanupError,
        )

        events: list[object] = []

        class RegistrationError(RuntimeError):
            pass

        class GuardReleaseError(RuntimeError):
            pass

        class FailingExitGuard:
            def __enter__(self):
                events.append("guard-enter")
                return self

            def __exit__(self, *_exc_info):
                events.append("guard-exit")
                raise GuardReleaseError("guard release failed")

        class FakeProcess:
            pid = 12345
            returncode = None
            stdout = None
            stderr = None

            def communicate(self, timeout=None):
                raise AssertionError(
                    "unconfirmed cleanup must stop algorithm runtime"
                )

        registration_error = RegistrationError(
            "process registration failed"
        )

        def process_started(_pid: int, _pgid: int) -> None:
            events.append("registration")
            raise registration_error

        def killpg(pgid: int, signum: int) -> None:
            events.append(("killpg", pgid, signum))

        with (
            patch(
                "scheduler.executor.subprocess.Popen",
                return_value=FakeProcess(),
            ),
            patch(
                "scheduler.executor.os.getpgid",
                return_value=67890,
            ),
            patch(
                "scheduler.executor.os.killpg",
                side_effect=killpg,
            ),
            patch(
                "scheduler.process_control."
                "_wait_for_process_group_exit",
                side_effect=(False, False),
            ),
        ):
            with self.assertRaises(
                ProcessRegistrationCleanupError
            ) as raised:
                _run_process_group(
                    ["demo"],
                    cwd=Path.cwd(),
                    env={},
                    timeout=7,
                    process_started=process_started,
                    process_start_guard=FailingExitGuard(),
                )

        self.assertIs(
            raised.exception.registration_error,
            registration_error,
        )
        self.assertIsInstance(
            raised.exception.__cause__,
            GuardReleaseError,
        )
        self.assertFalse(
            raised.exception.termination.confirmed_gone
        )
        self.assertEqual(
            events,
            [
                "guard-enter",
                "registration",
                ("killpg", 67890, signal.SIGTERM),
                ("killpg", 67890, signal.SIGKILL),
                "guard-exit",
            ],
        )

    def test_native_registration_precedes_post_fence_without_guard(
        self,
    ) -> None:
        from scheduler.executor import _run_process_group

        events: list[object] = []

        class FakeProcess:
            pid = 12345
            returncode = 0
            stdout = None
            stderr = None

            def communicate(self, timeout=None):
                events.append(("communicate", timeout))
                return "[]", ""

        def popen(*_args, **_kwargs):
            events.append("popen")
            return FakeProcess()

        def process_started(pid: int, pgid: int) -> None:
            events.append(("started", pid, pgid))

        def process_fence() -> None:
            events.append("epoch-fence")

        with (
            patch(
                "scheduler.executor.subprocess.Popen",
                side_effect=popen,
            ),
            patch(
                "scheduler.executor.os.getpgid",
                return_value=67890,
            ),
        ):
            completed = _run_process_group(
                ["demo"],
                cwd=Path.cwd(),
                env={},
                timeout=7,
                process_started=process_started,
                process_fence=process_fence,
            )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            events,
            [
                "epoch-fence",
                "popen",
                ("started", 12345, 67890),
                "epoch-fence",
                ("communicate", 7),
            ],
        )

    def test_native_shared_guard_prevents_unregistered_start_overlap(
        self,
    ) -> None:
        from scheduler.executor import _run_process_group

        events: list[tuple[str, int]] = []
        events_lock = threading.Lock()
        shared_lock = threading.Lock()
        first_popen_completed = threading.Event()
        second_enter_attempted = threading.Event()
        allow_first_registration = threading.Event()
        process_ids = iter((10101, 20202))

        class ObservedSharedGuard:
            def __enter__(self):
                if threading.current_thread().name == "second-start":
                    second_enter_attempted.set()
                shared_lock.acquire()
                return self

            def __exit__(self, *_exc_info):
                shared_lock.release()

        guard = ObservedSharedGuard()

        class FakeProcess:
            returncode = 0
            stdout = None
            stderr = None

            def __init__(self, pid: int) -> None:
                self.pid = pid

            def communicate(self, timeout=None):
                self.assert_guard_released()
                return "[]", ""

            @staticmethod
            def assert_guard_released() -> None:
                if shared_lock.locked():
                    raise AssertionError(
                        "algorithm runtime must not hold process-start guard"
                    )

        def popen(*_args, **_kwargs):
            pid = next(process_ids)
            with events_lock:
                events.append(("popen", pid))
            if pid == 10101:
                first_popen_completed.set()
            return FakeProcess(pid)

        def process_started(pid: int, _pgid: int) -> None:
            if pid == 10101:
                if not allow_first_registration.wait(timeout=2):
                    raise AssertionError(
                        "test did not release first registration"
                    )
            with events_lock:
                events.append(("registered", pid))

        errors: list[BaseException] = []

        def run() -> None:
            try:
                _run_process_group(
                    ["demo"],
                    cwd=Path.cwd(),
                    env={},
                    timeout=7,
                    process_started=process_started,
                    process_start_guard=guard,
                )
            except BaseException as exc:
                errors.append(exc)

        with (
            patch(
                "scheduler.executor.subprocess.Popen",
                side_effect=popen,
            ),
            patch(
                "scheduler.executor.os.getpgid",
                side_effect=lambda pid: pid,
            ),
        ):
            first = threading.Thread(target=run, name="first-start")
            second = threading.Thread(target=run, name="second-start")
            first.start()
            self.assertTrue(first_popen_completed.wait(timeout=2))
            second.start()
            self.assertTrue(second_enter_attempted.wait(timeout=2))
            with events_lock:
                self.assertEqual(events, [("popen", 10101)])
            allow_first_registration.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            events,
            [
                ("popen", 10101),
                ("registered", 10101),
                ("popen", 20202),
                ("registered", 20202),
            ],
        )

    def test_canonical_guard_poison_blocks_waiter_before_popen_and_db_fence(
        self,
    ) -> None:
        from scheduler.executor import _run_process_group
        from scheduler.process_control import (
            ProcessRegistrationCleanupError,
            ProcessStartGuard,
            ProcessStartGuardPoisonedError,
        )

        guard = ProcessStartGuard()
        first_registration_entered = threading.Event()
        release_first_failure = threading.Event()
        second_call_started = threading.Event()
        second_pre_fence = threading.Event()
        first_db_fence_started = threading.Event()
        release_first_db_fence = threading.Event()
        second_done = threading.Event()
        popen_calls: list[int] = []
        outcomes: dict[str, BaseException] = {}

        class RegistrationError(RuntimeError):
            pass

        class FakeProcess:
            pid = 10101
            returncode = None
            stdout = None
            stderr = None

            def communicate(self, timeout=None):
                raise AssertionError(
                    "poisoned process-start window must not communicate"
                )

        def popen(*_args, **_kwargs):
            popen_calls.append(len(popen_calls) + 1)
            if len(popen_calls) > 1:
                raise AssertionError(
                    "poisoned waiter must fail before Popen"
                )
            return FakeProcess()

        def first_process_started(_pid: int, _pgid: int) -> None:
            first_registration_entered.set()
            if not release_first_failure.wait(timeout=2):
                raise AssertionError("first failure was not released")
            raise RegistrationError("ledger registration failed")

        def run_first() -> None:
            try:
                _run_process_group(
                    ["first"],
                    cwd=Path.cwd(),
                    env={},
                    timeout=7,
                    process_started=first_process_started,
                    process_start_guard=guard,
                )
            except BaseException as exc:
                outcomes["first"] = exc
                first_db_fence_started.set()
                release_first_db_fence.wait(timeout=2)

        def run_second() -> None:
            second_call_started.set()
            try:
                _run_process_group(
                    ["second"],
                    cwd=Path.cwd(),
                    env={},
                    timeout=7,
                    process_fence=second_pre_fence.set,
                    process_start_guard=guard,
                )
            except BaseException as exc:
                outcomes["second"] = exc
            finally:
                second_done.set()

        with (
            patch(
                "scheduler.executor.subprocess.Popen",
                side_effect=popen,
            ),
            patch(
                "scheduler.executor.os.getpgid",
                return_value=10101,
            ),
            patch(
                "scheduler.executor.os.killpg",
            ),
            patch(
                "scheduler.process_control."
                "_wait_for_process_group_exit",
                side_effect=(False, False),
            ),
        ):
            first = threading.Thread(target=run_first)
            second = threading.Thread(target=run_second)
            first.start()
            self.assertTrue(
                first_registration_entered.wait(timeout=2)
            )
            second.start()
            self.assertTrue(second_call_started.wait(timeout=2))
            release_first_failure.set()
            self.assertTrue(first_db_fence_started.wait(timeout=2))
            self.assertTrue(
                second_done.wait(timeout=2),
                "poisoned waiter must not wait for DB fence persistence",
            )
            self.assertFalse(second_pre_fence.is_set())
            self.assertEqual(popen_calls, [1])
            release_first_db_fence.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertIsInstance(
            outcomes["first"],
            ProcessRegistrationCleanupError,
        )
        self.assertIsInstance(
            outcomes["second"],
            ProcessStartGuardPoisonedError,
        )
        self.assertTrue(guard.poisoned)

    def test_native_callback_failure_kills_group_before_reraising(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess

        events: list[object] = []

        class RegistrationError(RuntimeError):
            pass

        class FakeProcess:
            pid = 12345
            returncode = None
            stdout = None
            stderr = None

            def communicate(self, timeout=None):
                events.append(("communicate", timeout))
                return "[]", ""

            def wait(self, timeout=None):
                events.append(("wait", timeout))
                self.returncode = -signal.SIGTERM
                return self.returncode

        def process_started(pid: int, pgid: int) -> None:
            events.append(("started", pid, pgid))
            raise RegistrationError("ledger write failed")

        def killpg(pgid: int, signum: int) -> None:
            events.append(("killpg", pgid, signum))

        with (
            patch(
                "scheduler.executor.subprocess.Popen",
                return_value=FakeProcess(),
            ),
            patch("scheduler.executor.os.getpgid", return_value=67890),
            patch("scheduler.executor.os.killpg", side_effect=killpg),
            patch(
                "scheduler.process_control."
                "_wait_for_process_group_exit",
                return_value=True,
            ),
        ):
            with self.assertRaisesRegex(
                RegistrationError,
                "ledger write failed",
            ):
                run_scheme_subprocess(
                    "demo",
                    "2026-07-03",
                    algo_env="test_env",
                    timeout_sec=7,
                    process_started=process_started,
                )

        self.assertEqual(
            events,
            [
                ("started", 12345, 67890),
                ("killpg", 67890, signal.SIGTERM),
            ],
        )

    def test_native_callback_failure_fences_when_group_survives_kill(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess
        from scheduler.process_control import (
            ProcessRegistrationCleanupError,
        )

        events: list[object] = []

        class RegistrationError(RuntimeError):
            pass

        class FakeProcess:
            pid = 12345
            returncode = None
            stdout = None
            stderr = None

            def communicate(self, timeout=None):
                raise AssertionError(
                    "unregistered process must never enter communicate"
                )

        registration_error = RegistrationError("ledger write failed")

        def process_started(pid: int, pgid: int) -> None:
            events.append(("started", pid, pgid))
            raise registration_error

        def killpg(pgid: int, signum: int) -> None:
            events.append(("killpg", pgid, signum))

        with (
            patch(
                "scheduler.executor.subprocess.Popen",
                return_value=FakeProcess(),
            ),
            patch("scheduler.executor.os.getpgid", return_value=67890),
            patch("scheduler.executor.os.killpg", side_effect=killpg),
            patch(
                "scheduler.process_control."
                "_wait_for_process_group_exit",
                side_effect=(False, False),
            ),
        ):
            with self.assertRaises(
                ProcessRegistrationCleanupError
            ) as raised:
                run_scheme_subprocess(
                    "demo",
                    "2026-07-03",
                    algo_env="test_env",
                    timeout_sec=7,
                    process_started=process_started,
                )

        self.assertIs(raised.exception.registration_error, registration_error)
        self.assertFalse(
            raised.exception.termination.confirmed_gone
        )
        self.assertEqual(
            raised.exception.termination.process_group_id,
            67890,
        )
        self.assertEqual(
            events,
            [
                ("started", 12345, 67890),
                ("killpg", 67890, signal.SIGTERM),
                ("killpg", 67890, signal.SIGKILL),
            ],
        )

    def test_run_configured_scheme_forwards_process_callback_by_runtime(
        self,
    ) -> None:
        from scheduler.executor import run_configured_scheme
        from scheduler.process_control import ProcessStartGuard

        def callback(_pid: int, _pgid: int) -> None:
            return None

        def fence() -> None:
            return None

        process_start_guard = ProcessStartGuard()
        native_cfg = SimpleNamespace(
            runtime_type="native_adapter",
            scheme_id="native_trial",
        )
        blackbox_cfg = SimpleNamespace(
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
            scheme_id="blackbox_trial",
        )
        with patch(
            "scheduler.executor.run_scheme_subprocess",
            return_value=[],
        ) as native:
            run_configured_scheme(
                native_cfg,
                "2026-07-03",
                engine="engine",
                algo_env="test_env",
                timeout_sec=7,
                process_started=callback,
                process_fence=fence,
                process_start_guard=process_start_guard,
            )
        with patch(
            "scheduler.executor.run_blackbox_scheme_subprocess",
            return_value=[],
        ) as blackbox:
            run_configured_scheme(
                blackbox_cfg,
                "2026-07-03",
                engine="engine",
                algo_env="test_env",
                timeout_sec=7,
                process_started=callback,
                process_fence=fence,
                process_start_guard=process_start_guard,
            )

        self.assertIs(native.call_args.kwargs["process_started"], callback)
        self.assertIs(blackbox.call_args.kwargs["process_started"], callback)
        self.assertIs(native.call_args.kwargs["process_fence"], fence)
        self.assertIs(blackbox.call_args.kwargs["process_fence"], fence)
        self.assertIs(
            native.call_args.kwargs["process_start_guard"],
            process_start_guard,
        )
        self.assertIs(
            blackbox.call_args.kwargs["process_start_guard"],
            process_start_guard,
        )

    def test_run_configured_scheme_rejects_noncanonical_process_start_guard(
        self,
    ) -> None:
        from scheduler.executor import run_configured_scheme

        cfg = SimpleNamespace(
            runtime_type="native_adapter",
            scheme_id="native_trial",
        )
        with patch(
            "scheduler.executor.run_scheme_subprocess",
            return_value=[],
        ) as native:
            with self.assertRaisesRegex(
                TypeError,
                "ProcessStartGuard",
            ):
                run_configured_scheme(
                    cfg,
                    "2026-07-03",
                    engine="engine",
                    algo_env="test_env",
                    timeout_sec=7,
                    process_start_guard=threading.Lock(),
                )

        native.assert_not_called()

    def test_run_configured_scheme_forwards_explicit_blackbox_execution_token(
        self,
    ) -> None:
        from scheduler.executor import run_configured_scheme

        cfg = SimpleNamespace(
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
            scheme_id="blackbox_trial",
        )
        with patch(
            "scheduler.executor.run_blackbox_scheme_subprocess",
            return_value=[],
        ) as blackbox:
            run_configured_scheme(
                cfg,
                "2026-07-03",
                engine="engine",
                algo_env="test_env",
                timeout_sec=7,
                execution_token="scheduled-token_123",
            )

        self.assertEqual(
            blackbox.call_args.kwargs["execution_token"],
            "scheduled-token_123",
        )

    def test_run_configured_scheme_does_not_steal_blackbox_token_from_environment(
        self,
    ) -> None:
        from scheduler.executor import run_configured_scheme

        cfg = SimpleNamespace(
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
            scheme_id="blackbox_trial",
        )
        with (
            patch.dict(
                os.environ,
                {"BOND_SCHEDULE_EXECUTION_TOKEN": "stale-parent-token"},
            ),
            patch(
                "scheduler.executor.run_blackbox_scheme_subprocess",
                return_value=[],
            ) as blackbox,
        ):
            run_configured_scheme(
                cfg,
                "2026-07-03",
                engine="engine",
                algo_env="test_env",
                timeout_sec=7,
            )

        self.assertNotIn(
            "execution_token",
            blackbox.call_args.kwargs,
        )

    def test_run_configured_scheme_rejects_unsafe_execution_token_before_dispatch(
        self,
    ) -> None:
        from scheduler.executor import run_configured_scheme

        cfg = SimpleNamespace(
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
            scheme_id="blackbox_trial",
        )
        with patch(
            "scheduler.executor.run_blackbox_scheme_subprocess",
        ) as blackbox:
            with self.assertRaisesRegex(ValueError, "execution_token is unsafe"):
                run_configured_scheme(
                    cfg,
                    "2026-07-03",
                    engine="engine",
                    algo_env="test_env",
                    timeout_sec=7,
                    execution_token="unsafe token",
                )

        blackbox.assert_not_called()

    def test_run_scheme_subprocess_timeout_kills_process_group(self) -> None:
        from scheduler.executor import run_scheme_subprocess

        events: list[object] = []

        class FakeProcess:
            pid = 12345
            returncode = None

            def __init__(self) -> None:
                self.communicate_calls = 0

            def communicate(self, timeout=None):
                events.append(("communicate", timeout))
                self.communicate_calls += 1
                if self.communicate_calls == 1:
                    raise subprocess.TimeoutExpired(cmd=["conda"], timeout=timeout)
                return "", "timeout"

            def wait(self, timeout=None):
                self.returncode = -signal.SIGTERM
                return self.returncode

        fake_process = FakeProcess()
        with (
            patch(
                "scheduler.executor.subprocess.Popen",
                return_value=fake_process,
            ) as popen,
            patch(
                "scheduler.executor.os.getpgid",
                side_effect=lambda pid: (
                    events.append(("getpgid", pid)) or 67890
                ),
            ) as getpgid,
            patch(
                "scheduler.executor.os.killpg",
                side_effect=lambda pgid, signum: events.append(
                    ("killpg", pgid, signum)
                ),
            ) as killpg,
            patch(
                "scheduler.process_control."
                "_wait_for_process_group_exit",
                return_value=True,
            ),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_scheme_subprocess(
                    "demo",
                    "2026-07-03",
                    algo_env="test_env",
                    timeout_sec=1,
                )

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        getpgid.assert_called_once_with(12345)
        killpg.assert_called_once_with(67890, signal.SIGTERM)
        self.assertEqual(
            events,
            [
                ("getpgid", 12345),
                ("communicate", 1),
                ("killpg", 67890, signal.SIGTERM),
                ("communicate", 1),
            ],
        )

    def test_run_scheme_subprocess_timeout_drain_is_bounded(self) -> None:
        from scheduler.executor import run_scheme_subprocess

        class FakePipe:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class FakeProcess:
            pid = 12345
            returncode = None

            def __init__(self) -> None:
                self.communicate_timeouts: list[int | None] = []
                self.stdout = FakePipe()
                self.stderr = FakePipe()

            def communicate(self, timeout=None):
                self.communicate_timeouts.append(timeout)
                if len(self.communicate_timeouts) == 1:
                    raise subprocess.TimeoutExpired(cmd=["conda"], timeout=timeout)
                raise subprocess.TimeoutExpired(cmd=["conda"], timeout=timeout, output="partial", stderr="err")

            def wait(self, timeout=None):
                self.returncode = -signal.SIGTERM
                return self.returncode

        fake_process = FakeProcess()
        with (
            patch(
                "scheduler.executor.subprocess.Popen",
                return_value=fake_process,
            ),
            patch("scheduler.executor.os.getpgid", return_value=67890),
            patch("scheduler.executor.os.killpg"),
            patch(
                "scheduler.process_control."
                "_wait_for_process_group_exit",
                return_value=True,
            ),
        ):
            with self.assertRaises(subprocess.TimeoutExpired) as ctx:
                run_scheme_subprocess(
                    "demo",
                    "2026-07-03",
                    algo_env="test_env",
                    timeout_sec=7,
                )

        self.assertEqual(fake_process.communicate_timeouts, [7, 1])
        self.assertTrue(fake_process.stdout.closed)
        self.assertTrue(fake_process.stderr.closed)
        self.assertEqual(ctx.exception.output, "partial")
        self.assertEqual(ctx.exception.stderr, "err")

    def test_run_scheme_subprocess_disables_conda_output_capture(self) -> None:
        from scheduler.executor import run_scheme_subprocess

        class FakeProcess:
            pid = 12345
            returncode = 0
            stdout = None
            stderr = None

            def communicate(self, timeout=None):
                return "[]", ""

        with patch("scheduler.executor.subprocess.Popen", return_value=FakeProcess()) as popen:
            records = run_scheme_subprocess("demo", "2026-07-03", algo_env="test_env", timeout_sec=7)

        self.assertEqual(records, [])
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[:4], ["conda", "run", "--no-capture-output", "-n"])

    def test_execute_scheme_appends_predictions_without_serving_pointer(self) -> None:
        from scheduler.executor import execute_scheme
        from shared.models import PredictionRecord

        engine = _FakeEngine()
        cfg = SimpleNamespace(scheme_id="t1_daily", status="active", scheme_version="abc123", horizon=1)
        records = [
            PredictionRecord(
                scheme_id="t1_daily",
                target_tenor="5Y",
                horizon=1,
                predict_date="2026-06-05",
                target_date="2026-06-06",
                feature_date="2026-06-04",
                predicted_direction=1,
                extra={"feature_date": "2026-06-04"},
            ),
            PredictionRecord(
                scheme_id="t1_daily",
                target_tenor="10Y",
                horizon=1,
                predict_date="2026-06-05",
                target_date="2026-06-06",
                feature_date="2026-06-04",
                predicted_direction=-1,
                extra={"feature_date": "2026-06-04"},
            ),
        ]

        with patch("scheduler.executor.create_engine_from_env", return_value=engine):
            with patch("scheduler.executor._verify_scheme_activation", return_value=(True, "ok")):
                with patch("scheduler.executor._active_registry_targets", return_value={("5Y", 1), ("10Y", 1)}):
                    with patch("scheduler.executor.create_scheme_run", return_value=101) as create_run:
                        with patch("scheduler.executor.run_scheme_subprocess", return_value=records):
                            with patch("scheduler.executor.insert_run_predictions", return_value=2) as insert_predictions:
                                with patch("scheduler.executor.finish_scheme_run") as finish_run:
                                    with patch("scheduler.executor.write_run_log") as write_run_log:
                                        result = execute_scheme(cfg, "2026-06-05", algo_env="test_env")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.records_written, 2)
        self.assertEqual(result.run_id, 101)
        self.assertTrue(engine.disposed)
        create_run.assert_called_once_with(
            engine,
            scheme_id="t1_daily",
            predict_date="2026-06-05",
            scheme_version="abc123",
            runtime_type="native_adapter",
            run_type="active",
            prediction_phase="scheduled_live",
            records_expected=2,
        )
        written_records = insert_predictions.call_args.args[2]
        self.assertEqual([record.prediction_phase for record in written_records], ["scheduled_live", "scheduled_live"])
        self.assertEqual([record.extra["prediction_phase"] for record in written_records], ["scheduled_live", "scheduled_live"])
        insert_predictions.assert_called_once()
        self.assertEqual(insert_predictions.call_args.args[:2], (engine, 101))
        self.assertEqual(insert_predictions.call_args.kwargs["scheme_version"], "abc123")
        finish_run.assert_called_once()
        self.assertEqual(finish_run.call_args.kwargs["run_id"], 101)
        self.assertEqual(finish_run.call_args.kwargs["status"], "success")
        self.assertEqual(finish_run.call_args.kwargs["records_returned"], 2)
        self.assertEqual(finish_run.call_args.kwargs["records_written"], 2)
        write_run_log.assert_called_once()
        self.assertEqual(write_run_log.call_args.kwargs["run_id"], 101)

    def test_execute_scheme_uses_per_scheme_schedule_timeout(self) -> None:
        from scheduler.executor import execute_scheme
        from shared.models import PredictionRecord

        engine = _FakeEngine()
        cfg = SimpleNamespace(
            scheme_id="slow_daily",
            status="active",
            scheme_version="abc123",
            horizon=5,
            schedule=SimpleNamespace(timeout_sec=1800),
        )
        records = [
            PredictionRecord(
                scheme_id="slow_daily",
                target_tenor="10Y",
                horizon=5,
                predict_date="2026-07-03",
                target_date="2026-07-10",
                feature_date="2026-07-02",
                predicted_direction=1,
                extra={"feature_date": "2026-07-02"},
            )
        ]

        with patch("scheduler.executor.create_engine_from_env", return_value=engine):
            with patch("scheduler.executor._verify_scheme_activation", return_value=(True, "ok")):
                with patch("scheduler.executor._active_registry_targets", return_value={("10Y", 5)}):
                    with patch("scheduler.executor.create_scheme_run", return_value=201):
                        with patch("scheduler.executor.run_scheme_subprocess", return_value=records) as runner:
                            with patch("scheduler.executor.insert_run_predictions", return_value=1):
                                with patch("scheduler.executor.finish_scheme_run"):
                                    with patch("scheduler.executor.write_run_log"):
                                        result = execute_scheme(cfg, "2026-07-03", algo_env="test_env")

        self.assertEqual(result.status, "success")
        runner.assert_called_once_with(
            "slow_daily",
            "2026-07-03",
            algo_env="test_env",
            timeout_sec=1800,
        )

    def test_ledger_mode_daily_executor_fails_before_algorithm_process(
        self,
    ) -> None:
        from scheduler.executor import execute_scheme

        engine = _FakeEngine()
        cfg = SimpleNamespace(
            scheme_id="daily_fenced",
            status="active",
            scheme_version="v1",
            runtime_type="native_adapter",
            frequency="daily",
        )
        with (
            patch(
                "scheduler.executor.create_engine_from_env",
                return_value=engine,
            ),
            patch(
                "scheduler.executor._verify_scheme_activation",
                return_value=(True, "ok"),
            ),
            patch(
                "scheduler.executor._active_registry_targets",
                return_value={("5Y", 1)},
            ),
            patch.dict(
                os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
            ),
            patch(
                "scheduler.executor.create_scheme_run",
                side_effect=RuntimeError(
                    "scheduled_live daily creation requires a daily "
                    "ledger item in ledger mode"
                ),
            ) as create_run,
            patch(
                "scheduler.executor.run_configured_scheme",
            ) as algorithm,
            patch("scheduler.executor.write_run_log"),
        ):
            result = execute_scheme(
                cfg,
                "2026-07-24",
                algo_env="test_env",
            )

        self.assertEqual(result.status, "failed")
        self.assertIn("daily ledger item", result.error_msg or "")
        self.assertTrue(
            create_run.call_args.kwargs[
                "enforce_scheduled_live_ledger"
            ]
        )
        self.assertEqual(
            create_run.call_args.kwargs["schedule_frequency"],
            "daily",
        )
        algorithm.assert_not_called()
        self.assertTrue(engine.disposed)

    def test_daily_executor_without_explicit_mode_fails_before_run_creation(
        self,
    ) -> None:
        from scheduler.executor import execute_scheme

        engine = _FakeEngine()
        cfg = SimpleNamespace(
            scheme_id="daily_unowned",
            status="active",
            scheme_version="v1",
            runtime_type="native_adapter",
            frequency="daily",
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "scheduler.executor.create_engine_from_env",
                return_value=engine,
            ),
            patch(
                "scheduler.executor._verify_scheme_activation",
                return_value=(True, "ok"),
            ),
            patch(
                "scheduler.executor._active_registry_targets",
                return_value={("5Y", 1)},
            ),
            patch(
                "scheduler.executor.create_scheme_run",
            ) as create_run,
            patch(
                "scheduler.executor.run_configured_scheme",
            ) as algorithm,
            patch("scheduler.executor.write_run_log"),
        ):
            result = execute_scheme(
                cfg,
                "2026-07-24",
                algo_env="test_env",
            )

        self.assertEqual(result.status, "failed")
        self.assertIn("must be explicitly set", result.error_msg or "")
        create_run.assert_not_called()
        algorithm.assert_not_called()
        self.assertTrue(engine.disposed)

    def test_execute_scheme_rejects_records_outside_active_registry_targets(self) -> None:
        from scheduler.executor import execute_scheme
        from shared.models import PredictionRecord

        engine = _FakeEngine()
        cfg = SimpleNamespace(scheme_id="t1_daily", status="active", scheme_version="abc123", horizon=1)
        records = [
            PredictionRecord(
                scheme_id="t1_daily",
                target_tenor="7Y",
                horizon=1,
                predict_date="2026-06-05",
                target_date="2026-06-06",
                feature_date="2026-06-04",
                predicted_direction=1,
                extra={"feature_date": "2026-06-04"},
            )
        ]

        with patch("scheduler.executor.create_engine_from_env", return_value=engine):
            with patch("scheduler.executor._verify_scheme_activation", return_value=(True, "ok")):
                with patch("scheduler.executor._active_registry_targets", return_value={("5Y", 1)}):
                    with patch("scheduler.executor.create_scheme_run", return_value=102):
                        with patch("scheduler.executor.run_scheme_subprocess", return_value=records):
                            with patch("scheduler.executor.insert_run_predictions") as insert_predictions:
                                with patch("scheduler.executor.finish_scheme_run") as finish_run:
                                    with patch("scheduler.executor.write_run_log") as write_run_log:
                                        result = execute_scheme(cfg, "2026-06-05", algo_env="test_env")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.records_written, 0)
        self.assertEqual(
            result.error_msg,
            "live target mismatch: missing=[('5Y', 1)], extra=[('7Y', 1)], duplicates=[]",
        )
        insert_predictions.assert_not_called()
        finish_run.assert_called_once()
        self.assertEqual(finish_run.call_args.kwargs["status"], "failed")
        self.assertEqual(finish_run.call_args.kwargs["records_returned"], 1)
        self.assertEqual(finish_run.call_args.kwargs["records_written"], 0)
        write_run_log.assert_called_once()
        self.assertEqual(write_run_log.call_args.args[3], "failed")

    def test_execute_scheme_rejects_daily_records_with_stale_live_context(self) -> None:
        from scheduler.executor import execute_scheme
        from shared.models import PredictionRecord

        engine = _FakeEngine()
        cfg = SimpleNamespace(
            scheme_id="t5_daily",
            status="active",
            scheme_version="abc123",
            horizon=5,
            frequency="daily",
        )
        records = [
            PredictionRecord(
                scheme_id="t5_daily",
                target_tenor="10Y",
                horizon=5,
                predict_date="2026-07-07",
                target_date="2026-07-10",
                feature_date="2026-07-03",
                predicted_direction=1,
                extra={"feature_date": "2026-07-03"},
            )
        ]
        calendar = SimpleNamespace(
            previous_trading_day=lambda value: "2026-07-06",
            nth_trading_day_after=lambda value, count: "2026-07-13",
        )

        with patch("scheduler.executor.create_engine_from_env", return_value=engine):
            with patch("scheduler.executor.get_calendar", return_value=calendar, create=True):
                with patch("scheduler.executor._verify_scheme_activation", return_value=(True, "ok")):
                    with patch("scheduler.executor._active_registry_targets", return_value={("10Y", 5)}):
                        with patch("scheduler.executor.create_scheme_run", return_value=301):
                            with patch("scheduler.executor.run_scheme_subprocess", return_value=records):
                                with patch("scheduler.executor.insert_run_predictions") as insert_predictions:
                                    with patch("scheduler.executor.finish_scheme_run") as finish_run:
                                        with patch("scheduler.executor.write_run_log") as write_run_log:
                                            result = execute_scheme(cfg, "2026-07-07", algo_env="test_env")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.records_written, 0)
        self.assertIn("expected feature_date=2026-07-06", result.error_msg or "")
        self.assertIn("expected target_date=2026-07-13", result.error_msg or "")
        insert_predictions.assert_not_called()
        finish_run.assert_called_once()
        self.assertEqual(finish_run.call_args.kwargs["status"], "failed")
        self.assertEqual(finish_run.call_args.kwargs["records_returned"], 1)
        self.assertEqual(finish_run.call_args.kwargs["records_written"], 0)
        write_run_log.assert_called_once()
        self.assertEqual(write_run_log.call_args.args[3], "failed")

    def test_executor_does_not_write_serving_pointer(self) -> None:
        import scheduler.executor as executor

        self.assertNotIn("update_serving_pointer", inspect.getsource(executor))


class BlackboxExecutionApprovalTests(_ExplicitLegacyModeTestCase):
    @staticmethod
    def _config(
        *,
        scheme_version: str = "blackbox-version-1",
        version_status: str = "active",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            scheme_id="approved_blackbox",
            status="active",
            version_status=version_status,
            runtime_type="blackbox_v2",
            scheme_version=scheme_version,
            horizon=1,
            task_type="T+1",
            tenors=["10Y"],
            frequency="weekly",
            schedule=SimpleNamespace(timeout_sec=120),
            path=Path(__file__).resolve().parents[1] / "schemes" / "approved_blackbox",
        )

    @staticmethod
    def _record():
        from shared.models import PredictionRecord

        return PredictionRecord(
            scheme_id="approved_blackbox",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-07-20",
            target_date="2026-07-21",
            feature_date="2026-07-17",
            predicted_direction=1,
            extra={"feature_date": "2026-07-17", "data_snapshot_id": "snapshot-1"},
        )

    def test_blackbox_executor_fails_closed_for_every_one_sided_or_missing_state(self) -> None:
        from scheduler.executor import execute_scheme

        cases = [
            (
                "shadow",
                SimpleNamespace(
                    executable=False,
                    reason="version status is shadow, expected active",
                    version_status="shadow",
                    approved_by=None,
                    approved_at=None,
                ),
            ),
            (
                "active without approver",
                SimpleNamespace(
                    executable=False,
                    reason="version approved_by is null",
                    version_status="active",
                    approved_by=None,
                    approved_at="2026-07-20 08:30:00",
                ),
            ),
            (
                "active without approval time",
                SimpleNamespace(
                    executable=False,
                    reason="version approved_at is null",
                    version_status="active",
                    approved_by="release-owner",
                    approved_at=None,
                ),
            ),
            (
                "version mismatch",
                SimpleNamespace(
                    executable=False,
                    reason=(
                        "exact version not found: scheme_id=approved_blackbox "
                        "scheme_version=blackbox-version-1"
                    ),
                    version_status=None,
                    approved_by=None,
                    approved_at=None,
                ),
            ),
            (
                "registry only active",
                SimpleNamespace(
                    executable=False,
                    reason="version status is draft, expected active",
                    version_status="draft",
                    approved_by=None,
                    approved_at=None,
                ),
            ),
            (
                "config only active",
                SimpleNamespace(
                    executable=False,
                    reason="registry row missing: approved_blackbox__h1__10Y",
                    version_status="active",
                    approved_by="release-owner",
                    approved_at="2026-07-20 08:30:00",
                ),
            ),
        ]
        for label, approval in cases:
            with self.subTest(label=label):
                engine = _FakeEngine()
                cfg = self._config()
                with (
                    patch("scheduler.executor.create_engine_from_env", return_value=engine),
                    patch(
                        "scheduler.executor.read_blackbox_execution_approval",
                        return_value=approval,
                        create=True,
                    ) as approval_reader,
                    patch("scheduler.executor._verify_scheme_activation", return_value=(True, "ok")) as native_gate,
                    patch(
                        "scheduler.executor._active_registry_targets",
                        side_effect=[{("10Y", 1)}, {("10Y", 1)}],
                    ),
                    patch("scheduler.executor.create_scheme_run", return_value=501),
                    patch("scheduler.executor.run_configured_scheme", return_value=[self._record()]) as runner,
                    patch("scheduler.executor.attach_run_data_snapshot"),
                    patch("scheduler.executor.insert_run_predictions", return_value=1) as insert_predictions,
                    patch("scheduler.executor.finish_scheme_run"),
                    patch("scheduler.executor.write_run_log") as write_run_log,
                ):
                    result = execute_scheme(cfg, "2026-07-20", algo_env="test_env")

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.records_written, 0)
                self.assertEqual(
                    result.error_msg,
                    f"Blackbox V2 version is not production-approved: {approval.reason}",
                )
                approval_reader.assert_called_once_with(engine, cfg)
                native_gate.assert_not_called()
                runner.assert_not_called()
                insert_predictions.assert_not_called()
                self.assertEqual(write_run_log.call_args.args[3], "failed")
                self.assertTrue(engine.disposed)

    def test_blackbox_executor_blocks_incomplete_lifecycle_before_approval_read(self) -> None:
        from scheduler.executor import execute_scheme
        from shared.blackbox_v2.lifecycle import LifecycleJournal, LifecycleState, write_journal

        engine = _FakeEngine()
        cfg = self._config()
        project_root = cfg.path.parents[1]
        journal = LifecycleJournal.prepare(
            action="activate",
            scheme_id=cfg.scheme_id,
            scheme_version=cfg.scheme_version,
            harness_run_id="hr_1",
            previous=LifecycleState("paused", "shadow", "paused"),
            target=LifecycleState("active", "active", "active"),
            token_hash="hash",
        )
        path = write_journal(project_root, journal)
        try:
            with (
                patch("scheduler.executor.create_engine_from_env", return_value=engine),
                patch("scheduler.executor.read_blackbox_execution_approval") as approval_reader,
                patch("scheduler.executor.write_run_log") as write_run_log,
            ):
                result = execute_scheme(cfg, "2026-07-20", algo_env="test_env")
        finally:
            path.unlink(missing_ok=True)
            (path.parent / ".lock").unlink(missing_ok=True)
            path.parent.rmdir()

        self.assertEqual(result.status, "failed")
        self.assertIn("lifecycle journal", result.error_msg)
        approval_reader.assert_not_called()
        self.assertEqual(write_run_log.call_args.args[3], "failed")

    def test_blackbox_executor_rejects_active_config_with_shadow_version_before_algorithm(self) -> None:
        from scheduler.executor import execute_scheme

        engine = _FakeEngine()
        cfg = self._config(version_status="shadow")
        approval = SimpleNamespace(executable=True, reason="approved")
        with (
            patch("scheduler.executor.create_engine_from_env", return_value=engine),
            patch(
                "scheduler.executor.read_blackbox_execution_approval",
                return_value=approval,
            ) as approval_reader,
            patch(
                "scheduler.executor._active_registry_targets",
                side_effect=[{("10Y", 1)}, {("10Y", 1)}],
            ),
            patch("scheduler.executor.run_configured_scheme", return_value=[self._record()]) as runner,
            patch("scheduler.executor.create_scheme_run", return_value=503) as create_run,
            patch("scheduler.executor.attach_run_data_snapshot"),
            patch("scheduler.executor.complete_approved_blackbox_run", return_value=1),
            patch("scheduler.executor.finish_scheme_run"),
            patch("scheduler.executor.write_run_log") as write_run_log,
        ):
            result = execute_scheme(cfg, "2026-07-20", algo_env="test_env")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.records_written, 0)
        self.assertEqual(
            result.error_msg,
            "Blackbox V2 config version_status is shadow, expected active",
        )
        approval_reader.assert_not_called()
        create_run.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(write_run_log.call_args.args[3], "failed")
        self.assertTrue(engine.disposed)

    def test_fully_approved_blackbox_version_executes_and_writes_prediction(self) -> None:
        from scheduler.executor import execute_scheme

        engine = _FakeEngine()
        cfg = self._config()
        approval = SimpleNamespace(
            executable=True,
            reason="approved",
            version_status="active",
            approved_by="release-owner",
            approved_at="2026-07-20 08:30:00",
        )
        with (
            patch("scheduler.executor.create_engine_from_env", return_value=engine),
            patch(
                "scheduler.executor.read_blackbox_execution_approval",
                return_value=approval,
                create=True,
            ) as approval_reader,
            patch("scheduler.executor._verify_scheme_activation", return_value=(True, "ok")) as native_gate,
            patch(
                "scheduler.executor._active_registry_targets",
                side_effect=[{("10Y", 1)}, {("10Y", 1)}],
            ),
            patch("scheduler.executor.create_scheme_run", return_value=502) as create_run,
            patch("scheduler.executor.run_configured_scheme", return_value=[self._record()]) as runner,
            patch("scheduler.executor.attach_run_data_snapshot") as attach_snapshot,
            patch(
                "scheduler.executor.complete_approved_blackbox_run",
                return_value=1,
            ) as complete_run,
            patch("scheduler.executor.insert_run_predictions", return_value=1) as native_insert,
            patch("scheduler.executor.finish_scheme_run"),
            patch("scheduler.executor.write_run_log"),
        ):
            result = execute_scheme(cfg, "2026-07-20", algo_env="test_env")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.records_written, 1)
        approval_reader.assert_called_once_with(engine, cfg)
        native_gate.assert_not_called()
        create_run.assert_called_once_with(
            engine,
            scheme_id="approved_blackbox",
            predict_date="2026-07-20",
            scheme_version="blackbox-version-1",
            runtime_type="blackbox_v2",
            run_type="active",
            prediction_phase="scheduled_live",
            records_expected=1,
        )
        runner.assert_called_once_with(
            cfg,
            "2026-07-20",
            engine=engine,
            algo_env="test_env",
            timeout_sec=120,
        )
        attach_snapshot.assert_called_once_with(engine, run_id=502, data_snapshot_id="snapshot-1")
        complete_run.assert_called_once()
        self.assertEqual(complete_run.call_args.args[:2], (engine, cfg))
        self.assertEqual(complete_run.call_args.kwargs["run_id"], 502)
        self.assertEqual(complete_run.call_args.kwargs["scheme_version"], "blackbox-version-1")
        self.assertEqual(complete_run.call_args.kwargs["records_returned"], 1)
        native_insert.assert_not_called()
        self.assertTrue(engine.disposed)

    def test_blackbox_executor_forwards_historical_snapshot_mode_explicitly(self) -> None:
        from scheduler.executor import execute_scheme

        engine = _FakeEngine()
        cfg = self._config()
        approval = SimpleNamespace(executable=True, reason="approved")
        with (
            patch("scheduler.executor.create_engine_from_env", return_value=engine),
            patch(
                "scheduler.executor.read_blackbox_execution_approval",
                return_value=approval,
            ),
            patch(
                "scheduler.executor._active_registry_targets",
                return_value={("10Y", 1)},
            ),
            patch("scheduler.executor.create_scheme_run", return_value=505),
            patch(
                "scheduler.executor.run_configured_scheme",
                return_value=[self._record()],
            ) as runner,
            patch("scheduler.executor.attach_run_data_snapshot"),
            patch(
                "scheduler.executor.complete_approved_blackbox_run",
                return_value=1,
            ) as complete_run,
            patch("scheduler.executor.write_run_log"),
        ):
            result = execute_scheme(
                cfg,
                "2026-07-20",
                algo_env="test_env",
                prediction_phase="gray_live",
                blackbox_snapshot_mode="historical_as_of_replay",
            )

        self.assertEqual(result.status, "success")
        runner.assert_called_once_with(
            cfg,
            "2026-07-20",
            engine=engine,
            algo_env="test_env",
            timeout_sec=120,
            blackbox_snapshot_mode="historical_as_of_replay",
            expected_generation_id=None,
            expected_refresh_date=None,
        )
        self.assertTrue(complete_run.call_args.kwargs["insert_only_predictions"])

    def test_blackbox_revocation_after_subprocess_fails_with_zero_predictions(self) -> None:
        from scheduler.executor import execute_scheme

        revocations = [
            "version status is paused, expected active",
            "registry approved_blackbox__h1__10Y status is paused, expected active",
        ]
        for reason in revocations:
            with self.subTest(reason=reason):
                engine = _FakeEngine()
                cfg = self._config()
                approval = SimpleNamespace(
                    executable=True,
                    reason="approved",
                    version_status="active",
                    approved_by="release-owner",
                    approved_at="2026-07-20 08:30:00",
                )
                final_error = RuntimeError(
                    f"Blackbox V2 version is not production-approved: {reason}"
                )
                with (
                    patch("scheduler.executor.create_engine_from_env", return_value=engine),
                    patch(
                        "scheduler.executor.read_blackbox_execution_approval",
                        return_value=approval,
                    ),
                    patch(
                        "scheduler.executor._active_registry_targets",
                        return_value={("10Y", 1)},
                    ),
                    patch("scheduler.executor.create_scheme_run", return_value=504),
                    patch("scheduler.executor.run_configured_scheme", return_value=[self._record()]) as runner,
                    patch("scheduler.executor.attach_run_data_snapshot"),
                    patch(
                        "scheduler.executor.complete_approved_blackbox_run",
                        side_effect=final_error,
                    ) as complete_run,
                    patch("scheduler.executor.insert_run_predictions", return_value=1) as native_insert,
                    patch("scheduler.executor.fail_scheme_run_atomic") as fail_run,
                    patch("scheduler.executor.write_run_log"),
                ):
                    result = execute_scheme(cfg, "2026-07-20", algo_env="test_env")

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.records_written, 0)
                self.assertEqual(result.error_msg, str(final_error))
                runner.assert_called_once()
                complete_run.assert_called_once()
                native_insert.assert_not_called()
                fail_run.assert_called_once()
                self.assertEqual(fail_run.call_args.kwargs["records_returned"], 1)

    def test_native_executor_keeps_existing_activation_gate(self) -> None:
        from scheduler.executor import execute_scheme

        engine = _FakeEngine()
        cfg = SimpleNamespace(
            scheme_id="native_scheme",
            status="active",
            runtime_type="native_adapter",
            scheme_version="native-version-1",
            horizon=1,
            frequency="weekly",
        )
        record = self._record()
        record = record.__class__(
            scheme_id="native_scheme",
            target_tenor=record.target_tenor,
            horizon=record.horizon,
            predict_date=record.predict_date,
            target_date=record.target_date,
            feature_date=record.feature_date,
            predicted_direction=record.predicted_direction,
            extra={"feature_date": record.feature_date},
        )
        with (
            patch("scheduler.executor.create_engine_from_env", return_value=engine),
            patch(
                "scheduler.executor.read_blackbox_execution_approval",
                create=True,
            ) as approval_reader,
            patch("scheduler.executor._verify_scheme_activation", return_value=(True, "ok")) as native_gate,
            patch(
                "scheduler.executor._active_registry_targets",
                side_effect=[{("10Y", 1)}, {("10Y", 1)}],
            ),
            patch("scheduler.executor.create_scheme_run", return_value=503),
            patch("scheduler.executor.run_configured_scheme", return_value=[record]),
            patch("scheduler.executor.insert_run_predictions", return_value=1),
            patch("scheduler.executor.finish_scheme_run"),
            patch("scheduler.executor.write_run_log"),
        ):
            result = execute_scheme(cfg, "2026-07-20", algo_env="test_env")

        self.assertEqual(result.status, "success")
        approval_reader.assert_not_called()
        native_gate.assert_called_once_with(engine, "native_scheme", "native-version-1")


class ExecutorTargetCompletenessTests(_ExplicitLegacyModeTestCase):
    @staticmethod
    def _record(
        target_tenor: str,
        *,
        scheme_id: str = "target_complete",
        horizon: int = 1,
        predicted_direction: int = 1,
        feature_date: str | None = "2026-07-02",
    ):
        from shared.models import PredictionRecord

        extra = {"feature_date": feature_date} if feature_date is not None else None
        return PredictionRecord(
            scheme_id=scheme_id,
            target_tenor=target_tenor,
            horizon=horizon,
            predict_date="2026-07-03",
            target_date="2026-07-06",
            feature_date=feature_date,
            predicted_direction=predicted_direction,
            extra=extra,
        )

    def _execute(
        self,
        records,
        *,
        active_targets: set[tuple[str, int]],
        current_active_targets: set[tuple[str, int]] | None = None,
        records_written: int | None = None,
        finish_side_effect=None,
        write_log_side_effect=None,
    ):
        from scheduler.executor import execute_scheme

        engine = _FakeEngine()
        cfg = SimpleNamespace(
            scheme_id="target_complete",
            status="active",
            scheme_version="version-1",
            horizon=1,
        )
        written = len(records) if records_written is None else records_written
        current_targets = active_targets if current_active_targets is None else current_active_targets
        with (
            patch("scheduler.executor.create_engine_from_env", return_value=engine),
            patch("scheduler.executor._verify_scheme_activation", return_value=(True, "ok")),
            patch(
                "scheduler.executor._active_registry_targets",
                side_effect=[active_targets, current_targets],
            ) as active_targets_reader,
            patch("scheduler.executor.create_scheme_run", return_value=401) as create_run,
            patch("scheduler.executor.run_scheme_subprocess", return_value=records) as runner,
            patch("scheduler.executor.insert_run_predictions", return_value=written) as insert_predictions,
            patch("scheduler.executor.finish_scheme_run", side_effect=finish_side_effect) as finish_run,
            patch("scheduler.executor.write_run_log", side_effect=write_log_side_effect) as write_run_log,
            patch("scheduler.executor.logger.exception") as logger_exception,
        ):
            result = execute_scheme(cfg, "2026-07-03", algo_env="test_env")
        return SimpleNamespace(
            result=result,
            create_run=create_run,
            active_targets_reader=active_targets_reader,
            runner=runner,
            insert_predictions=insert_predictions,
            finish_run=finish_run,
            write_run_log=write_run_log,
            logger_exception=logger_exception,
        )

    def test_execute_scheme_rejects_empty_return_for_active_target(self) -> None:
        execution = self._execute([], active_targets={("5Y", 1)})

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(
            execution.result.error_msg,
            "live target mismatch: missing=[('5Y', 1)], extra=[], duplicates=[]",
        )
        execution.insert_predictions.assert_not_called()
        self.assertEqual(execution.finish_run.call_args.kwargs["records_returned"], 0)
        self.assertEqual(execution.finish_run.call_args.kwargs["records_written"], 0)

    def test_execute_scheme_rejects_missing_target(self) -> None:
        execution = self._execute(
            [self._record("5Y")],
            active_targets={("5Y", 1), ("10Y", 1)},
        )

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(
            execution.result.error_msg,
            "live target mismatch: missing=[('10Y', 1)], extra=[], duplicates=[]",
        )
        execution.insert_predictions.assert_not_called()
        self.assertEqual(execution.finish_run.call_args.kwargs["records_returned"], 1)

    def test_execute_scheme_rejects_duplicate_target(self) -> None:
        execution = self._execute(
            [self._record("5Y"), self._record("5Y")],
            active_targets={("5Y", 1)},
        )

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(
            execution.result.error_msg,
            "live target mismatch: missing=[], extra=[], duplicates=[('5Y', 1, 2)]",
        )
        execution.insert_predictions.assert_not_called()
        self.assertEqual(execution.finish_run.call_args.kwargs["records_returned"], 2)

    def test_execute_scheme_rejects_extra_target(self) -> None:
        execution = self._execute(
            [self._record("5Y"), self._record("7Y")],
            active_targets={("5Y", 1)},
        )

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(
            execution.result.error_msg,
            "live target mismatch: missing=[], extra=[('7Y', 1)], duplicates=[]",
        )
        execution.insert_predictions.assert_not_called()
        self.assertEqual(execution.finish_run.call_args.kwargs["records_returned"], 2)

    def test_execute_scheme_rejects_unexpected_empty_active_targets(self) -> None:
        execution = self._execute([], active_targets=set())

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(
            execution.result.error_msg,
            "active registry targets empty for scheme target_complete: missing=[], extra=[], duplicates=[]",
        )
        execution.runner.assert_not_called()
        execution.insert_predictions.assert_not_called()
        execution.create_run.assert_called_once_with(
            execution.create_run.call_args.args[0],
            scheme_id="target_complete",
            predict_date="2026-07-03",
            scheme_version="version-1",
            runtime_type="native_adapter",
            run_type="active",
            prediction_phase="scheduled_live",
            records_expected=0,
        )
        self.assertIsNone(execution.finish_run.call_args.kwargs["records_returned"])

    def test_execute_scheme_records_raw_count_when_normalization_fails(self) -> None:
        execution = self._execute(
            [self._record("5Y", feature_date=None)],
            active_targets={("5Y", 1)},
        )

        self.assertEqual(execution.result.status, "failed")
        self.assertIn("missing feature_date", execution.result.error_msg or "")
        execution.insert_predictions.assert_not_called()
        self.assertEqual(execution.finish_run.call_args.kwargs["records_returned"], 1)

    def test_execute_scheme_rejects_target_paused_during_run(self) -> None:
        execution = self._execute(
            [self._record("5Y")],
            active_targets={("5Y", 1)},
            current_active_targets=set(),
        )

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(
            execution.result.error_msg,
            "active registry targets changed during run: initial=[('5Y', 1)], current=[]",
        )
        execution.insert_predictions.assert_not_called()
        self.assertEqual(execution.active_targets_reader.call_count, 2)
        self.assertEqual(execution.create_run.call_args.kwargs["records_expected"], 1)

    def test_execute_scheme_rejects_target_added_during_run(self) -> None:
        execution = self._execute(
            [self._record("5Y")],
            active_targets={("5Y", 1)},
            current_active_targets={("5Y", 1), ("10Y", 1)},
        )

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(
            execution.result.error_msg,
            "active registry targets changed during run: "
            "initial=[('5Y', 1)], current=[('10Y', 1), ('5Y', 1)]",
        )
        execution.insert_predictions.assert_not_called()
        self.assertEqual(execution.active_targets_reader.call_count, 2)
        self.assertEqual(execution.create_run.call_args.kwargs["records_expected"], 1)

    def test_execute_scheme_accepts_complete_flat_record(self) -> None:
        execution = self._execute(
            [self._record("5Y", predicted_direction=0)],
            active_targets={("5Y", 1)},
        )

        self.assertEqual(execution.result.status, "success")
        self.assertEqual(execution.result.records_written, 1)
        inserted = execution.insert_predictions.call_args.args[2]
        self.assertEqual([record.predicted_direction for record in inserted], [0])
        self.assertEqual(execution.finish_run.call_args.kwargs["records_returned"], 1)
        self.assertEqual(execution.finish_run.call_args.kwargs["records_written"], 1)

    def test_execute_scheme_preserves_written_count_when_first_finish_fails(self) -> None:
        execution = self._execute(
            [self._record("5Y")],
            active_targets={("5Y", 1)},
            finish_side_effect=[RuntimeError("initial finish failed"), None],
        )

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(execution.result.records_written, 1)
        self.assertEqual(execution.result.error_msg, "initial finish failed")
        execution.insert_predictions.assert_called_once()
        self.assertEqual(execution.finish_run.call_count, 2)
        first_finish = execution.finish_run.call_args_list[0].kwargs
        second_finish = execution.finish_run.call_args_list[1].kwargs
        self.assertEqual(first_finish["status"], "success")
        self.assertEqual(first_finish["records_written"], 1)
        self.assertEqual(second_finish["status"], "failed")
        self.assertEqual(second_finish["records_written"], 1)
        self.assertEqual(second_finish["error_message"], "initial finish failed")

    def test_execute_scheme_keeps_success_when_run_log_write_fails(self) -> None:
        execution = self._execute(
            [self._record("5Y")],
            active_targets={("5Y", 1)},
            write_log_side_effect=[RuntimeError("run log failed"), None],
        )

        self.assertEqual(execution.result.status, "success")
        self.assertEqual(execution.result.records_written, 1)
        self.assertIsNone(execution.result.error_msg)
        self.assertEqual(execution.finish_run.call_count, 1)
        self.assertEqual(execution.finish_run.call_args.kwargs["status"], "success")
        self.assertEqual(execution.write_run_log.call_count, 1)
        execution.logger_exception.assert_called_once()

    def test_execute_scheme_preserves_business_error_when_failure_audits_fail(self) -> None:
        try:
            execution = self._execute(
                [self._record("5Y", feature_date=None)],
                active_targets={("5Y", 1)},
                finish_side_effect=RuntimeError("finish audit failed"),
                write_log_side_effect=RuntimeError("log audit failed"),
            )
        except RuntimeError as exc:
            self.fail(f"audit exception escaped instead of preserving business error: {exc}")

        self.assertEqual(execution.result.status, "failed")
        self.assertEqual(execution.result.records_written, 0)
        self.assertTrue(
            (execution.result.error_msg or "").startswith(
                "record target_complete/5Y missing feature_date"
            )
        )
        self.assertIn(
            "finish_scheme_run audit failed: finish audit failed",
            execution.result.error_msg or "",
        )
        self.assertIn(
            "write_run_log audit failed: log audit failed",
            execution.result.error_msg or "",
        )
        execution.finish_run.assert_called_once()
        execution.write_run_log.assert_called_once()
        self.assertEqual(execution.logger_exception.call_count, 2)

    def test_execute_scheme_marks_partial_with_all_three_counts(self) -> None:
        execution = self._execute(
            [self._record("5Y"), self._record("10Y")],
            active_targets={("5Y", 1), ("10Y", 1)},
            records_written=1,
        )

        self.assertEqual(execution.result.status, "partial")
        self.assertEqual(execution.result.records_written, 1)
        self.assertEqual(execution.result.error_msg, "expected=2, returned=2, written=1")
        self.assertEqual(execution.finish_run.call_args.kwargs["status"], "partial")
        self.assertEqual(execution.finish_run.call_args.kwargs["records_returned"], 2)
        self.assertEqual(execution.finish_run.call_args.kwargs["records_written"], 1)
        self.assertEqual(
            execution.finish_run.call_args.kwargs["error_message"],
            "expected=2, returned=2, written=1",
        )


if __name__ == "__main__":
    unittest.main()
