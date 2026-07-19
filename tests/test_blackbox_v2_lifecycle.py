from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


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

    def test_lifecycle_lock_times_out_and_can_be_reacquired_after_release(self) -> None:
        from shared.blackbox_v2.lifecycle import (
            LifecycleLockTimeout,
            lifecycle_operation_lock,
        )

        holder_ready = threading.Event()
        release_holder = threading.Event()

        def hold_lock() -> None:
            with lifecycle_operation_lock(self.root, "trial", timeout_sec=1):
                holder_ready.set()
                self.assertTrue(release_holder.wait(timeout=2))

        with ThreadPoolExecutor(max_workers=2) as pool:
            holder = pool.submit(hold_lock)
            self.assertTrue(holder_ready.wait(timeout=1))
            contender = pool.submit(
                lambda: lifecycle_operation_lock(
                    self.root,
                    "trial",
                    timeout_sec=0.05,
                    poll_interval_sec=0.005,
                ).__enter__()
            )
            with self.assertRaises(LifecycleLockTimeout):
                contender.result(timeout=1)
            release_holder.set()
            holder.result(timeout=1)

        with lifecycle_operation_lock(self.root, "trial", timeout_sec=0.1):
            pass

    def test_lifecycle_lock_releases_after_body_exception(self) -> None:
        from shared.blackbox_v2.lifecycle import lifecycle_operation_lock

        with self.assertRaisesRegex(RuntimeError, "injected body failure"):
            with lifecycle_operation_lock(self.root, "trial", timeout_sec=0.1):
                raise RuntimeError("injected body failure")

        with lifecycle_operation_lock(self.root, "trial", timeout_sec=0.1):
            pass

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

    def test_each_phase_transition_records_an_immutable_utc_timestamp(self) -> None:
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

        self.assertIsInstance(getattr(journal, "phase_events", None), tuple)
        self.assertEqual(
            [event.phase for event in journal.phase_events],
            ["prepared", "config_written", "db_committed", "verified"],
        )
        for event in journal.phase_events:
            parsed = datetime.fromisoformat(event.at)
            self.assertIsNotNone(parsed.tzinfo)
            self.assertEqual(parsed.utcoffset().total_seconds(), 0)
        for terminal_phase in ("compensated", "unresolved"):
            with self.subTest(terminal_phase=terminal_phase):
                terminal = LifecycleJournal.prepare(
                    action="activate",
                    scheme_id="trial",
                    scheme_version="abc123",
                    harness_run_id="hr_1",
                    previous=previous,
                    target=target,
                    token_hash="hash",
                ).transition(terminal_phase, error="injected")
                self.assertEqual(
                    [event.phase for event in terminal.phase_events],
                    ["prepared", terminal_phase],
                )
                self.assertIsNotNone(datetime.fromisoformat(terminal.phase_events[-1].at).tzinfo)

    def test_load_journal_accepts_legacy_payload_without_phase_events(self) -> None:
        from shared.blackbox_v2.lifecycle import LifecycleJournal, load_journal, write_journal

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
        path = write_journal(self.root, journal)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("phase_events", None)
        path.write_text(json.dumps(payload), encoding="utf-8")

        loaded = load_journal(path)

        self.assertEqual(loaded.phase, "prepared")
        self.assertEqual(getattr(loaded, "phase_events", None), ())

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

    def test_replay_loser_cannot_mutate_target_config_or_database(self) -> None:
        from harness.authorization import issue_token, mark_token_used, parse_token
        from shared.blackbox_v2.lifecycle import (
            LifecycleOperationError,
            LifecycleState,
            load_journal,
            perform_lifecycle_transition,
        )

        config_path = self.root / "schemes" / "trial" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        original = "status: paused\nversion_status: shadow\n"
        config_path.write_text(original, encoding="utf-8")
        previous = LifecycleState("paused", "shadow", "paused")
        target = LifecycleState("active", "active", "active")
        actual = {"state": previous}
        database_targets: list[LifecycleState] = []
        auth = parse_token(issue_token("trial", "blackbox_activate"))
        used_path = self.root / "reports" / "harness" / ".used_authorization_tokens.json"
        mark_token_used(auth, used_path)

        def apply_database(state: LifecycleState) -> None:
            database_targets.append(state)
            actual["state"] = state

        with self.assertRaises(LifecycleOperationError) as caught:
            perform_lifecycle_transition(
                project_root=self.root,
                config_path=config_path,
                action="blackbox_activate",
                scheme_id="trial",
                scheme_version="abc123",
                harness_run_id="hr_1",
                previous=previous,
                target=target,
                compensation=previous,
                token_hash="hash",
                consume_authorization=lambda: mark_token_used(auth, used_path),
                apply_database=apply_database,
                read_state=lambda: actual["state"],
            )

        self.assertEqual(database_targets, [])
        self.assertEqual(config_path.read_text(encoding="utf-8"), original)
        journal = load_journal(caught.exception.journal_path)
        self.assertEqual(journal.phase, "compensated")
        self.assertIn("already used", journal.error)

    def test_malformed_replay_store_cannot_mutate_config_or_database(self) -> None:
        from harness.authorization import issue_token, mark_token_used, parse_token
        from shared.blackbox_v2.lifecycle import (
            LifecycleOperationError,
            LifecycleState,
            load_journal,
            perform_lifecycle_transition,
        )

        config_path = self.root / "schemes" / "trial" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        original = "status: paused\nversion_status: shadow\n"
        config_path.write_text(original, encoding="utf-8")
        previous = LifecycleState("paused", "shadow", "paused")
        target = LifecycleState("active", "active", "active")
        actual = {"state": previous}
        database_targets: list[LifecycleState] = []
        auth = parse_token(issue_token("trial", "blackbox_activate"))
        used_path = self.root / "reports" / "harness" / ".used_authorization_tokens.json"
        used_path.parent.mkdir(parents=True)
        used_path.write_text('{"unexpected": "shape"}\n', encoding="utf-8")

        def apply_database(state: LifecycleState) -> None:
            database_targets.append(state)
            actual["state"] = state

        with self.assertRaises(LifecycleOperationError) as caught:
            perform_lifecycle_transition(
                project_root=self.root,
                config_path=config_path,
                action="blackbox_activate",
                scheme_id="trial",
                scheme_version="abc123",
                harness_run_id="hr_1",
                previous=previous,
                target=target,
                compensation=previous,
                token_hash="hash",
                consume_authorization=lambda: mark_token_used(auth, used_path),
                apply_database=apply_database,
                read_state=lambda: actual["state"],
            )

        self.assertEqual(database_targets, [])
        self.assertEqual(config_path.read_text(encoding="utf-8"), original)
        journal = load_journal(caught.exception.journal_path)
        self.assertEqual(journal.phase, "compensated")
        self.assertIn("replay store", journal.error)

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

    def test_unresolved_reconcile_preserves_terminal_journal_and_links_new_audit(self) -> None:
        from shared.blackbox_v2.lifecycle import (
            LifecycleJournal,
            load_journal,
            pending_journals,
            reconcile_journal,
            write_journal,
        )

        previous, target = self._states()
        config_path = self.root / "schemes" / "trial" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("status: active\nversion_status: active\n", encoding="utf-8")
        original = LifecycleJournal.prepare(
            action="activate",
            scheme_id="trial",
            scheme_version="abc123",
            harness_run_id="hr_1",
            previous=previous,
            target=target,
            token_hash="hash",
        ).transition("unresolved", error="database compensation failed")
        original_path = write_journal(self.root, original)
        actual = {"state": target}

        restored = reconcile_journal(
            original_path,
            config_path=config_path,
            apply_database=lambda state: actual.__setitem__("state", state),
            read_state=lambda: actual["state"],
        )

        preserved = load_journal(original_path)
        journals = [load_journal(path) for path in original_path.parent.glob("*.json")]
        linked = [journal for journal in journals if journal.operation_id != original.operation_id]
        self.assertEqual(restored, previous)
        self.assertEqual(preserved.phase, "unresolved")
        self.assertEqual(preserved.error, "database compensation failed")
        self.assertIsNone(preserved.reconciled_by)
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0].action, "lifecycle_reconcile")
        self.assertEqual(linked[0].reconciliation_of, original.operation_id)
        self.assertEqual(linked[0].phase, "verified")
        self.assertEqual(
            [event.phase for event in getattr(linked[0], "phase_events", ())],
            ["prepared", "config_written", "db_committed", "verified"],
        )
        self.assertEqual(pending_journals(self.root, "trial"), [])

    def test_failed_reconcile_child_remains_audit_and_root_can_be_retried(self) -> None:
        from shared.blackbox_v2.lifecycle import (
            LifecycleJournal,
            load_journal,
            pending_journals,
            reconcile_journal,
            write_journal,
        )

        previous, target = self._states()
        config_path = self.root / "schemes" / "trial" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("status: active\nversion_status: active\n", encoding="utf-8")
        original = LifecycleJournal.prepare(
            action="activate",
            scheme_id="trial",
            scheme_version="abc123",
            harness_run_id="hr_1",
            previous=previous,
            target=target,
            token_hash="original-token-hash",
        ).transition("unresolved", error="database compensation failed")
        original_path = write_journal(self.root, original)
        original_bytes = original_path.read_bytes()
        actual = {"state": target}

        with self.assertRaisesRegex(RuntimeError, "reconciliation failed"):
            reconcile_journal(
                original_path,
                config_path=config_path,
                apply_database=lambda _state: (_ for _ in ()).throw(
                    RuntimeError("transient database failure")
                ),
                read_state=lambda: actual["state"],
                token_hash="first-reconcile-token-hash",
            )

        first_attempt_paths = [
            path for path in original_path.parent.glob("*.json") if path != original_path
        ]
        self.assertEqual(len(first_attempt_paths), 1)
        first_attempt_bytes = first_attempt_paths[0].read_bytes()
        first_attempt = load_journal(first_attempt_paths[0])
        self.assertEqual(first_attempt.phase, "unresolved")
        self.assertEqual(first_attempt.reconciliation_of, original.operation_id)
        pending = pending_journals(self.root, "trial")
        self.assertEqual([(path, journal.operation_id) for path, journal in pending], [
            (original_path, original.operation_id)
        ])

        restored = reconcile_journal(
            pending[0][0],
            config_path=config_path,
            apply_database=lambda state: actual.__setitem__("state", state),
            read_state=lambda: actual["state"],
            token_hash="second-reconcile-token-hash",
        )

        all_journals = [load_journal(path) for path in original_path.parent.glob("*.json")]
        attempts = [journal for journal in all_journals if journal.reconciliation_of == original.operation_id]
        self.assertEqual(restored, previous)
        self.assertEqual(actual["state"], previous)
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual(first_attempt_paths[0].read_bytes(), first_attempt_bytes)
        self.assertEqual(sorted(journal.phase for journal in attempts), ["unresolved", "verified"])
        self.assertEqual(pending_journals(self.root, "trial"), [])

    def test_only_matching_verified_reconcile_child_resolves_pending_root(self) -> None:
        from shared.blackbox_v2.lifecycle import (
            LifecycleJournal,
            pending_journals,
            write_journal,
        )

        previous, target = self._states()
        original = LifecycleJournal.prepare(
            action="activate",
            scheme_id="trial",
            scheme_version="abc123",
            harness_run_id="hr_1",
            previous=previous,
            target=target,
            token_hash="original",
        ).transition("unresolved", error="failed")
        original_path = write_journal(self.root, original)
        unrelated = LifecycleJournal.prepare(
            action="activate",
            scheme_id="trial",
            scheme_version="different-version",
            harness_run_id="hr_other",
            previous=target,
            target=previous,
            token_hash="unrelated",
            reconciliation_of=original.operation_id,
        ).transition("config_written").transition("db_committed").transition("verified")
        write_journal(self.root, unrelated)

        self.assertEqual(pending_journals(self.root, "trial")[0][0], original_path)

    def test_atomic_config_write_failure_is_compensated_without_active_residue(self) -> None:
        from shared.blackbox_v2.lifecycle import (
            LifecycleOperationError,
            load_journal,
            perform_lifecycle_transition,
        )

        previous, target = self._states()
        config_path = self.root / "schemes" / "trial" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        original_text = "status: paused\nversion_status: shadow\n"
        config_path.write_text(original_text, encoding="utf-8")
        actual = {"state": previous}

        with patch(
            "shared.blackbox_v2.lifecycle.atomic_update_config",
            side_effect=OSError("injected config replace failure"),
        ):
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
                    read_state=lambda: actual["state"],
                )

        self.assertTrue(caught.exception.compensated)
        self.assertEqual(load_journal(caught.exception.journal_path).phase, "compensated")
        self.assertEqual(config_path.read_text(encoding="utf-8"), original_text)
        self.assertEqual(actual["state"], previous)

    def test_final_verified_journal_write_failure_compensates_without_active_residue(self) -> None:
        from shared.blackbox_v2 import lifecycle

        previous, target = self._states()
        config_path = self.root / "schemes" / "trial" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        original_text = "status: paused\nversion_status: shadow\n"
        config_path.write_text(original_text, encoding="utf-8")
        actual = {"state": previous}
        real_write = lifecycle.write_journal
        writes: list[str] = []

        def fail_final_write(project_root, journal):
            writes.append(journal.phase)
            if journal.phase == "verified":
                raise OSError("injected final journal fsync failure")
            return real_write(project_root, journal)

        with patch("shared.blackbox_v2.lifecycle.write_journal", side_effect=fail_final_write):
            with self.assertRaises(lifecycle.LifecycleOperationError) as caught:
                lifecycle.perform_lifecycle_transition(
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
                    read_state=lambda: actual["state"],
                )

        self.assertIn("verified", writes)
        self.assertTrue(caught.exception.compensated)
        self.assertEqual(lifecycle.load_journal(caught.exception.journal_path).phase, "compensated")
        self.assertEqual(config_path.read_text(encoding="utf-8"), original_text)
        self.assertEqual(actual["state"], previous)


if __name__ == "__main__":
    unittest.main()
