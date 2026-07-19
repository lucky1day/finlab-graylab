from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class BlackboxLifecycleJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _states(self):
        from shared.blackbox_v2.lifecycle import LifecycleState

        return (
            LifecycleState("paused", "shadow", "paused"),
            LifecycleState("active", "active", "active"),
        )

    def test_journal_round_trip_contains_hash_but_no_raw_token(self) -> None:
        from shared.blackbox_v2.lifecycle import LifecycleJournal, load_journal, write_journal

        previous, target = self._states()
        raw_token = "raw-super-secret-token"
        journal = LifecycleJournal.prepare(
            action="activate",
            scheme_id="trial",
            scheme_version="abc123",
            harness_run_id="hr_1",
            previous=previous,
            target=target,
            token_hash="token-sha256",
        )

        path = write_journal(self.root, journal)
        loaded = load_journal(path)

        self.assertEqual(loaded, journal)
        payload = path.read_text(encoding="utf-8")
        self.assertIn("token-sha256", payload)
        self.assertNotIn(raw_token, payload)
        self.assertEqual(
            path.parent,
            self.root / "backtest_artifacts" / "blackbox_v2_lifecycle" / "trial",
        )

    def test_phase_transitions_are_strict(self) -> None:
        from shared.blackbox_v2.lifecycle import LifecycleJournal

        previous, target = self._states()
        journal = LifecycleJournal.prepare(
            action="activate",
            scheme_id="trial",
            scheme_version="abc123",
            harness_run_id="hr_1",
            previous=previous,
            target=target,
            token_hash="hash",
        )
        journal = journal.transition("config_written")
        journal = journal.transition("db_committed")
        journal = journal.transition("verified")
        self.assertEqual(journal.phase, "verified")
        with self.assertRaisesRegex(ValueError, "invalid lifecycle phase transition"):
            journal.transition("prepared")

    def test_transition_failure_compensates_to_previous_state(self) -> None:
        from shared.blackbox_v2.lifecycle import (
            LifecycleOperationError,
            LifecycleState,
            load_journal,
            perform_lifecycle_transition,
        )

        config_path = self.root / "schemes" / "trial" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("status: paused\nversion_status: shadow\n", encoding="utf-8")
        previous = LifecycleState("paused", "shadow", "paused")
        target = LifecycleState("active", "active", "active")
        actual = {"state": previous}
        consumed: list[str] = []

        def apply_db(state: LifecycleState) -> None:
            actual["state"] = state
            if state == target:
                raise RuntimeError("injected DB failure")

        with self.assertRaises(LifecycleOperationError) as caught:
            perform_lifecycle_transition(
                project_root=self.root,
                config_path=config_path,
                action="activate",
                scheme_id="trial",
                scheme_version="abc123",
                harness_run_id="hr_1",
                previous=previous,
                target=target,
                compensation=previous,
                token_hash="hash",
                consume_authorization=lambda: consumed.append("used"),
                apply_database=apply_db,
                read_state=lambda: actual["state"],
            )

        journal = load_journal(caught.exception.journal_path)
        self.assertTrue(caught.exception.compensated)
        self.assertEqual(journal.phase, "compensated")
        self.assertEqual(consumed, ["used"])
        self.assertEqual(actual["state"], previous)
        self.assertEqual(
            config_path.read_text(encoding="utf-8"),
            "status: paused\nversion_status: shadow\n",
        )

    def test_authorization_is_consumed_only_after_prepared_journal_is_durable(self) -> None:
        from shared.blackbox_v2.lifecycle import (
            LifecycleState,
            pending_journals,
            perform_lifecycle_transition,
        )

        config_path = self.root / "schemes" / "trial" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("status: paused\nversion_status: shadow\n", encoding="utf-8")
        previous = LifecycleState("paused", "shadow", "paused")
        target = LifecycleState("active", "active", "active")
        actual = {"state": previous}
        observed: list[str] = []

        def consume() -> None:
            journals = pending_journals(self.root, "trial")
            observed.append(journals[0][1].phase)

        perform_lifecycle_transition(
            project_root=self.root,
            config_path=config_path,
            action="activate",
            scheme_id="trial",
            scheme_version="abc123",
            harness_run_id="hr_1",
            previous=previous,
            target=target,
            compensation=previous,
            token_hash="hash",
            consume_authorization=consume,
            apply_database=lambda state: actual.__setitem__("state", state),
            read_state=lambda: actual["state"],
        )

        self.assertEqual(observed, ["prepared"])

    def test_verification_failure_after_db_commit_is_compensated(self) -> None:
        from shared.blackbox_v2.lifecycle import (
            LifecycleOperationError,
            LifecycleState,
            load_journal,
            perform_lifecycle_transition,
        )

        config_path = self.root / "schemes" / "trial" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("status: paused\nversion_status: shadow\n", encoding="utf-8")
        previous = LifecycleState("paused", "shadow", "paused")
        target = LifecycleState("active", "active", "active")
        actual = {"state": previous, "reads": 0}

        def read_state() -> LifecycleState:
            actual["reads"] += 1
            if actual["reads"] == 2:
                return previous
            return actual["state"]

        with self.assertRaises(LifecycleOperationError) as caught:
            perform_lifecycle_transition(
                project_root=self.root,
                config_path=config_path,
                action="activate",
                scheme_id="trial",
                scheme_version="abc123",
                harness_run_id="hr_1",
                previous=previous,
                target=target,
                compensation=previous,
                token_hash="hash",
                consume_authorization=lambda: None,
                apply_database=lambda state: actual.__setitem__("state", state),
                read_state=read_state,
            )

        self.assertTrue(caught.exception.compensated)
        self.assertEqual(load_journal(caught.exception.journal_path).phase, "compensated")
        self.assertEqual(actual["state"], previous)

    def test_failed_compensation_is_unresolved_and_blocks_execution(self) -> None:
        from shared.blackbox_v2.lifecycle import (
            LifecycleOperationError,
            LifecycleState,
            assert_lifecycle_clear,
            load_journal,
            perform_lifecycle_transition,
        )

        config_path = self.root / "schemes" / "trial" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("status: paused\nversion_status: shadow\n", encoding="utf-8")
        previous = LifecycleState("paused", "shadow", "paused")
        target = LifecycleState("active", "active", "active")

        def apply_db(_state: LifecycleState) -> None:
            raise RuntimeError("database unavailable")

        with self.assertRaises(LifecycleOperationError) as caught:
            perform_lifecycle_transition(
                project_root=self.root,
                config_path=config_path,
                action="activate",
                scheme_id="trial",
                scheme_version="abc123",
                harness_run_id="hr_1",
                previous=previous,
                target=target,
                compensation=previous,
                token_hash="hash",
                consume_authorization=lambda: None,
                apply_database=apply_db,
                read_state=lambda: previous,
            )

        self.assertFalse(caught.exception.compensated)
        self.assertEqual(load_journal(caught.exception.journal_path).phase, "unresolved")
        with self.assertRaisesRegex(RuntimeError, "unresolved lifecycle journal"):
            assert_lifecycle_clear(self.root, "trial")

    def test_manual_reconcile_only_restores_previous_state(self) -> None:
        from shared.blackbox_v2.lifecycle import (
            LifecycleJournal,
            LifecycleState,
            load_journal,
            reconcile_journal,
            write_journal,
        )

        config_path = self.root / "schemes" / "trial" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("status: active\nversion_status: active\n", encoding="utf-8")
        previous = LifecycleState("paused", "shadow", "paused")
        target = LifecycleState("active", "active", "active")
        journal = LifecycleJournal.prepare(
            action="activate",
            scheme_id="trial",
            scheme_version="abc123",
            harness_run_id="hr_1",
            previous=previous,
            target=target,
            token_hash="hash",
        ).transition("config_written")
        path = write_journal(self.root, journal)
        actual = {"state": target}

        result = reconcile_journal(
            path,
            config_path=config_path,
            apply_database=lambda state: actual.__setitem__("state", state),
            read_state=lambda: actual["state"],
        )

        self.assertEqual(result, previous)
        self.assertEqual(load_journal(path).phase, "compensated")
        self.assertEqual(actual["state"], previous)
        self.assertNotIn("active", config_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
