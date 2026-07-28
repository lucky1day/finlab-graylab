from __future__ import annotations

import dataclasses
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch


class IsolatedReplayProcessBoundaryProbeTests(unittest.TestCase):
    def _registered_process(
        self,
        process_id: int,
        *,
        base_scheme_id: str = "native-test-scheme",
        input_compatibility: str = "generation_v1",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            item_id=11,
            run_id=21,
            execution_token="opaque-token",
            process_id=process_id,
            process_group_id=process_id,
            base_scheme_id=base_scheme_id,
            input_compatibility=input_compatibility,
        )

    def _probe(
        self,
        *,
        registered_processes=(),
        processes=(),
        active_item_ids=(11,),
        operator_pid=9000,
        operator_pgid=None,
        operator_uid=501,
        include_operator=True,
        allow_backend=False,
    ):
        from scheduler import daily_control_plane_probe as probe_module

        normalized_operator_pgid = (
            operator_pid
            if operator_pgid is None
            else operator_pgid
        )
        read_registered = Mock(return_value=registered_processes)
        process_rows = [
            {
                "pid": 1,
                "ppid": 0,
                "pgid": 1,
                "uid": 0,
                "command": "/sbin/launchd",
            },
        ]
        if include_operator:
            process_rows.append(
                {
                    "pid": operator_pid,
                    "ppid": 1,
                    "pgid": normalized_operator_pgid,
                    "uid": operator_uid,
                    "command": "python replay-operator",
                }
            )
        process_rows.extend(processes)
        with (
            patch(
                "scheduler.repository."
                "read_current_replay_attempt_processes",
                read_registered,
            ),
            patch.object(
                probe_module,
                "_read_process_table",
                return_value=process_rows,
            ),
            patch.object(
                probe_module.os,
                "getpid",
                return_value=operator_pid,
            ),
        ):
            report = (
                probe_module.probe_isolated_replay_process_boundary(
                    object(),
                    service_uid=501,
                    occurrence_id=31,
                    active_item_ids=active_item_ids,
                    allow_backend=allow_backend,
                )
            )
        return report, read_registered

    def test_allows_registered_leader_and_same_uid_group_member(
        self,
    ) -> None:
        registered = self._registered_process(4200)
        report, _read_registered = self._probe(
            registered_processes=(registered,),
            processes=(
                {
                    "pid": 4201,
                    "ppid": 4200,
                    "pgid": 4200,
                    "uid": 501,
                    "command": "python child-without-project-markers",
                },
                {
                    "pid": 4200,
                    "ppid": 9000,
                    "pgid": 4200,
                    "uid": 501,
                    "command": "python -m scheduler.scheme_runner",
                },
            ),
        )

        self.assertTrue(report.boundary_clear)
        self.assertEqual(report.registered_leader_process_ids, (4200,))
        self.assertEqual(report.registered_process_group_ids, (4200,))
        self.assertEqual(
            tuple(
                (row.process_id, row.classification)
                for row in report.allowed_processes
            ),
            (
                (4200, "registered_leader"),
                (4201, "registered_process_group_member"),
                (9000, "operator"),
            ),
        )
        self.assertEqual(report.blocked_processes, ())
        self.assertNotIn(
            "scheduler.scheme_runner",
            repr(dataclasses.asdict(report)),
        )

    def test_blocks_unregistered_platform_process_with_same_command(
        self,
    ) -> None:
        report, _read_registered = self._probe(
            processes=(
                {
                    "pid": 4300,
                    "ppid": 1,
                    "pgid": 4300,
                    "uid": 501,
                    "command": "python -m scheduler.scheme_runner",
                },
            ),
        )

        self.assertFalse(report.boundary_clear)
        self.assertEqual(
            tuple(
                (row.process_id, row.classification)
                for row in report.allowed_processes
            ),
            ((9000, "operator"),),
        )
        self.assertEqual(len(report.blocked_processes), 1)
        finding = report.blocked_processes[0]
        self.assertEqual(finding.process_id, 4300)
        self.assertEqual(
            finding.classification,
            "unregistered_daily_platform_process",
        )
        self.assertFalse(hasattr(finding, "command"))

    def test_allows_exact_backend_entrypoint_when_explicit(self) -> None:
        report, _read_registered = self._probe(
            processes=(
                {
                    "pid": 4100,
                    "ppid": 1,
                    "pgid": 4100,
                    "uid": 501,
                    "command": (
                        "/usr/bin/python /usr/bin/conda run "
                        "--no-capture-output -n bfl_service "
                        "uvicorn backend.main:app --port 8100"
                    ),
                },
                {
                    "pid": 4101,
                    "ppid": 4100,
                    "pgid": 4100,
                    "uid": 501,
                    "command": (
                        "/opt/bfl/bin/python /opt/bfl/bin/uvicorn "
                        "backend.main:app --port 8100"
                    ),
                },
            ),
            allow_backend=True,
        )

        self.assertTrue(report.boundary_clear)
        self.assertEqual(
            tuple(
                (row.process_id, row.classification)
                for row in report.allowed_processes
            ),
            (
                (4100, "display_backend"),
                (4101, "display_backend"),
                (9000, "operator"),
            ),
        )

    def test_backend_marker_cannot_hide_scheduler_process(self) -> None:
        report, _read_registered = self._probe(
            processes=(
                {
                    "pid": 4300,
                    "ppid": 1,
                    "pgid": 4300,
                    "uid": 501,
                    "command": (
                        "python -m scheduler.scheme_runner "
                        "--note backend.main"
                    ),
                },
            ),
            allow_backend=True,
        )

        self.assertFalse(report.boundary_clear)
        self.assertEqual(
            tuple(
                (row.process_id, row.classification)
                for row in report.blocked_processes
            ),
            ((4300, "unregistered_daily_platform_process"),),
        )

    def test_backend_tokens_in_arguments_cannot_hide_project_process(
        self,
    ) -> None:
        report, _read_registered = self._probe(
            processes=(
                {
                    "pid": 4300,
                    "ppid": 1,
                    "pgid": 4300,
                    "uid": 501,
                    "command": (
                        "python /opt/bond-factor-lab/harness/"
                        "not_backend.py --note uvicorn "
                        "backend.main:app"
                    ),
                },
                {
                    "pid": 4400,
                    "ppid": 1,
                    "pgid": 4400,
                    "uid": 501,
                    "command": (
                        "python /opt/bond-factor-lab/harness/"
                        "not_backend.py --note -m backend.main"
                    ),
                },
            ),
            allow_backend=True,
        )

        self.assertFalse(report.boundary_clear)
        self.assertEqual(
            tuple(
                (row.process_id, row.classification)
                for row in report.blocked_processes
            ),
            (
                (4300, "unregistered_daily_platform_process"),
                (4400, "unregistered_daily_platform_process"),
            ),
        )

    def test_blocks_old_or_other_occurrence_process_not_returned_by_ledger(
        self,
    ) -> None:
        registered = self._registered_process(4200)
        report, _read_registered = self._probe(
            registered_processes=(registered,),
            processes=(
                {
                    "pid": 4300,
                    "ppid": 9000,
                    "pgid": 4300,
                    "uid": 501,
                    "command": (
                        "python -m scheduler.scheme_runner "
                        "--scheme-id same-scheme"
                    ),
                },
                {
                    "pid": 4200,
                    "ppid": 9000,
                    "pgid": 4200,
                    "uid": 501,
                    "command": (
                        "python -m scheduler.scheme_runner "
                        "--scheme-id same-scheme"
                    ),
                },
            ),
        )

        self.assertEqual(
            tuple(row.process_id for row in report.allowed_processes),
            (4200, 9000),
        )
        self.assertEqual(
            tuple(
                (row.process_id, row.classification)
                for row in report.blocked_processes
            ),
            ((4300, "unregistered_replay_descendant"),),
        )

    def test_blocks_other_uid_even_when_pid_or_pgid_is_registered(
        self,
    ) -> None:
        registered = self._registered_process(4200)
        report, _read_registered = self._probe(
            registered_processes=(registered,),
            processes=(
                {
                    "pid": 4200,
                    "ppid": 9000,
                    "pgid": 4200,
                    "uid": 0,
                    "command": "python -m scheduler.scheme_runner",
                },
                {
                    "pid": 4201,
                    "ppid": 4200,
                    "pgid": 4200,
                    "uid": 502,
                    "command": "python child",
                },
            ),
        )

        self.assertFalse(report.boundary_clear)
        self.assertEqual(
            tuple(row.process_id for row in report.allowed_processes),
            (9000,),
        )
        self.assertEqual(
            tuple(
                (row.process_id, row.uid, row.classification)
                for row in report.blocked_processes
            ),
            (
                (4200, 0, "registered_identity_uid_mismatch"),
                (4201, 502, "registered_identity_uid_mismatch"),
            ),
        )

    def test_empty_active_set_allows_no_replay_process_identity(
        self,
    ) -> None:
        report, read_registered = self._probe(
            active_item_ids=(),
            processes=(
                {
                    "pid": 4200,
                    "ppid": 9000,
                    "pgid": 4200,
                    "uid": 501,
                    "command": "python -m scheduler.scheme_runner",
                },
            ),
        )

        self.assertEqual(report.active_item_ids, ())
        self.assertFalse(report.boundary_clear)
        self.assertEqual(
            tuple(row.process_id for row in report.blocked_processes),
            (4200,),
        )
        read_registered.assert_called_once_with(
            unittest.mock.ANY,
            occurrence_id=31,
            active_item_ids=(),
        )

    def test_current_operator_pid_is_allowed(
        self,
    ) -> None:
        report, _read_registered = self._probe(operator_pid=9000)

        self.assertTrue(report.boundary_clear)
        self.assertEqual(len(report.allowed_processes), 1)
        self.assertEqual(
            report.allowed_processes[0].classification,
            "operator",
        )

    def test_blocks_root_or_other_uid_operator(self) -> None:
        for operator_uid in (0, 502):
            with self.subTest(operator_uid=operator_uid):
                report, _read_registered = self._probe(
                    operator_uid=operator_uid,
                )

                self.assertFalse(report.boundary_clear)
                self.assertEqual(report.allowed_processes, ())
                self.assertEqual(len(report.blocked_processes), 1)
                finding = report.blocked_processes[0]
                self.assertEqual(finding.process_id, 9000)
                self.assertEqual(finding.uid, operator_uid)
                self.assertEqual(
                    finding.classification,
                    "operator_uid_mismatch",
                )

    def test_fails_closed_when_operator_pid_is_missing(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "^operator process identity is not uniquely observable$",
        ):
            self._probe(include_operator=False)

    def test_fails_closed_on_operator_and_registered_identity_collision(
        self,
    ) -> None:
        collisions = (
            (
                self._registered_process(9000),
                8000,
            ),
            (
                self._registered_process(8000),
                8000,
            ),
        )
        for registered, operator_pgid in collisions:
            with (
                self.subTest(
                    registered_pid=registered.process_id,
                    operator_pgid=operator_pgid,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "^operator and registered replay process "
                    "identities collide$",
                ),
            ):
                self._probe(
                    registered_processes=(registered,),
                    operator_pgid=operator_pgid,
                )

    def test_ignores_unrelated_non_platform_process(
        self,
    ) -> None:
        report, _read_registered = self._probe(
            processes=(
                {
                    "pid": 7000,
                    "ppid": 1,
                    "pgid": 7000,
                    "uid": 0,
                    "command": "/usr/sbin/ordinary-daemon",
                },
            ),
        )

        self.assertTrue(report.boundary_clear)
        self.assertEqual(
            tuple(row.process_id for row in report.allowed_processes),
            (9000,),
        )
        self.assertEqual(report.blocked_processes, ())

    def test_blocks_unregistered_descendant_without_platform_tokens(
        self,
    ) -> None:
        from scheduler.daily_control_plane_probe import (
            _is_daily_platform_process,
        )

        real_command = (
            "python -c bootstrap bash "
            "daily_project/src/run_daily.sh --date 2026-07-27"
        )
        self.assertFalse(_is_daily_platform_process(real_command))

        registered = self._registered_process(4200)
        report, _read_registered = self._probe(
            registered_processes=(registered,),
            processes=(
                {
                    "pid": 4200,
                    "ppid": 1,
                    "pgid": 4200,
                    "uid": 501,
                    "command": "python registered-leader",
                },
                {
                    "pid": 7100,
                    "ppid": 4200,
                    "pgid": 7100,
                    "uid": 501,
                    "command": real_command,
                },
            ),
        )

        self.assertFalse(report.boundary_clear)
        self.assertEqual(len(report.blocked_processes), 1)
        finding = report.blocked_processes[0]
        self.assertEqual(finding.process_id, 7100)
        self.assertEqual(
            finding.classification,
            "unregistered_replay_descendant",
        )
        self.assertFalse(hasattr(finding, "command"))

    def test_allows_new_session_descendant_for_approved_0629_item(
        self,
    ) -> None:
        from scheduler.daily_policy import (
            APPROVED_0629_LIVE_SOURCE_SCHEMES,
        )

        approved_scheme_id = sorted(
            APPROVED_0629_LIVE_SOURCE_SCHEMES
        )[0]
        registered = self._registered_process(
            4200,
            base_scheme_id=approved_scheme_id,
            input_compatibility="live_source_0629",
        )
        report, _read_registered = self._probe(
            registered_processes=(registered,),
            processes=(
                {
                    "pid": 4200,
                    "ppid": 9000,
                    "pgid": 4200,
                    "uid": 501,
                    "command": "python registered-0629-leader",
                },
                {
                    "pid": 7100,
                    "ppid": 4200,
                    "pgid": 7100,
                    "uid": 501,
                    "command": "python source-parent-watchdog",
                },
                {
                    "pid": 7101,
                    "ppid": 7100,
                    "pgid": 7100,
                    "uid": 501,
                    "command": "bash daily_project/src/run_daily.sh",
                },
            ),
        )

        self.assertTrue(report.boundary_clear)
        self.assertEqual(report.blocked_processes, ())
        self.assertEqual(
            tuple(
                (row.process_id, row.classification)
                for row in report.allowed_processes
            ),
            (
                (4200, "registered_leader"),
                (7100, "approved_live_source_descendant"),
                (7101, "approved_live_source_descendant"),
                (9000, "operator"),
            ),
        )

    def test_does_not_allow_new_session_descendant_for_unapproved_item(
        self,
    ) -> None:
        registered = self._registered_process(
            4200,
            base_scheme_id="not-approved-0629",
            input_compatibility="live_source_0629",
        )
        report, _read_registered = self._probe(
            registered_processes=(registered,),
            processes=(
                {
                    "pid": 4200,
                    "ppid": 9000,
                    "pgid": 4200,
                    "uid": 501,
                    "command": "python registered-leader",
                },
                {
                    "pid": 7100,
                    "ppid": 4200,
                    "pgid": 7100,
                    "uid": 501,
                    "command": "python unexpected-new-session",
                },
            ),
        )

        self.assertFalse(report.boundary_clear)
        self.assertEqual(
            tuple(
                (row.process_id, row.classification)
                for row in report.blocked_processes
            ),
            ((7100, "unregistered_replay_descendant"),),
        )

    def test_requires_live_source_mode_for_approved_0629_descendant(
        self,
    ) -> None:
        from scheduler.daily_policy import (
            APPROVED_0629_LIVE_SOURCE_SCHEMES,
        )

        registered = self._registered_process(
            4200,
            base_scheme_id=sorted(
                APPROVED_0629_LIVE_SOURCE_SCHEMES
            )[0],
            input_compatibility="generation_v1",
        )
        report, _read_registered = self._probe(
            registered_processes=(registered,),
            processes=(
                {
                    "pid": 4200,
                    "ppid": 9000,
                    "pgid": 4200,
                    "uid": 501,
                    "command": "python registered-leader",
                },
                {
                    "pid": 7100,
                    "ppid": 4200,
                    "pgid": 7100,
                    "uid": 501,
                    "command": "python unexpected-new-session",
                },
            ),
        )

        self.assertFalse(report.boundary_clear)
        self.assertEqual(
            report.blocked_processes[0].classification,
            "unregistered_replay_descendant",
        )

    def test_blocks_other_uid_approved_0629_descendant(
        self,
    ) -> None:
        from scheduler.daily_policy import (
            APPROVED_0629_LIVE_SOURCE_SCHEMES,
        )

        registered = self._registered_process(
            4200,
            base_scheme_id=sorted(
                APPROVED_0629_LIVE_SOURCE_SCHEMES
            )[0],
            input_compatibility="live_source_0629",
        )
        report, _read_registered = self._probe(
            registered_processes=(registered,),
            processes=(
                {
                    "pid": 4200,
                    "ppid": 9000,
                    "pgid": 4200,
                    "uid": 501,
                    "command": "python registered-leader",
                },
                {
                    "pid": 7100,
                    "ppid": 4200,
                    "pgid": 7100,
                    "uid": 502,
                    "command": "python wrong-uid-new-session",
                },
            ),
        )

        self.assertFalse(report.boundary_clear)
        self.assertEqual(
            tuple(
                (row.process_id, row.uid, row.classification)
                for row in report.blocked_processes
            ),
            (
                (
                    7100,
                    502,
                    "approved_live_source_descendant_uid_mismatch",
                ),
            ),
        )

    def test_allows_same_uid_operator_process_group_descendant(
        self,
    ) -> None:
        report, _read_registered = self._probe(
            processes=(
                {
                    "pid": 7300,
                    "ppid": 9000,
                    "pgid": 9000,
                    "uid": 501,
                    "command": (
                        "/usr/local/mysql/bin/mysqld "
                        "--defaults-file=/private/replay/my.cnf"
                    ),
                },
            ),
        )

        self.assertTrue(report.boundary_clear)
        self.assertEqual(
            tuple(
                (row.process_id, row.classification)
                for row in report.allowed_processes
            ),
            (
                (7300, "operator_process_group_member"),
                (9000, "operator"),
            ),
        )

    def test_blocks_other_uid_operator_process_group_descendant(
        self,
    ) -> None:
        report, _read_registered = self._probe(
            processes=(
                {
                    "pid": 7300,
                    "ppid": 9000,
                    "pgid": 9000,
                    "uid": 0,
                    "command": "/usr/local/mysql/bin/mysqld",
                },
            ),
        )

        self.assertFalse(report.boundary_clear)
        self.assertEqual(len(report.blocked_processes), 1)
        finding = report.blocked_processes[0]
        self.assertEqual(finding.process_id, 7300)
        self.assertEqual(
            finding.classification,
            "operator_process_group_uid_mismatch",
        )

    def test_does_not_allow_operator_group_member_without_lineage(
        self,
    ) -> None:
        report, _read_registered = self._probe(
            processes=(
                {
                    "pid": 7300,
                    "ppid": 1,
                    "pgid": 9000,
                    "uid": 501,
                    "command": "python unrelated-without-platform-token",
                },
            ),
        )

        self.assertTrue(report.boundary_clear)
        self.assertEqual(
            tuple(row.process_id for row in report.allowed_processes),
            (9000,),
        )
        self.assertEqual(report.blocked_processes, ())

    def test_blocks_old_or_fenced_descendant_tree(
        self,
    ) -> None:
        registered = self._registered_process(4200)
        report, _read_registered = self._probe(
            registered_processes=(registered,),
            processes=(
                {
                    "pid": 4200,
                    "ppid": 9000,
                    "pgid": 4200,
                    "uid": 501,
                    "command": "python current-leader",
                },
                {
                    "pid": 4300,
                    "ppid": 9000,
                    "pgid": 4300,
                    "uid": 501,
                    "command": "bash daily_project/src/run_daily.sh",
                },
                {
                    "pid": 4301,
                    "ppid": 4300,
                    "pgid": 4300,
                    "uid": 501,
                    "command": "python -c worker",
                },
            ),
        )

        self.assertEqual(
            tuple(
                (row.process_id, row.classification)
                for row in report.blocked_processes
            ),
            (
                (4300, "unregistered_replay_descendant"),
                (4301, "unregistered_replay_descendant"),
            ),
        )

    def test_fails_closed_on_ppid_cycle_or_missing_parent(
        self,
    ) -> None:
        unsafe_process_sets = (
            (
                {
                    "pid": 7100,
                    "ppid": 7101,
                    "pgid": 7100,
                    "uid": 501,
                    "command": "python child-a",
                },
                {
                    "pid": 7101,
                    "ppid": 7100,
                    "pgid": 7101,
                    "uid": 501,
                    "command": "python child-b",
                },
            ),
            (
                {
                    "pid": 7200,
                    "ppid": 7999,
                    "pgid": 7200,
                    "uid": 501,
                    "command": "python missing-parent",
                },
            ),
        )
        for processes in unsafe_process_sets:
            with (
                self.subTest(processes=processes),
                self.assertRaisesRegex(
                    RuntimeError,
                    "^OS process table parent relationships "
                    "are malformed$",
                ),
            ):
                self._probe(processes=processes)

    def test_strict_process_table_parser_includes_ppid(self) -> None:
        from scheduler import daily_control_plane_probe as probe_module

        sampler = MagicMock(pid=7300, returncode=0)
        sampler.communicate.return_value = (
            (
                "1 0 1 0 /sbin/launchd\n"
                "9000 1 9000 501 python replay-operator\n"
                "7300 9000 9000 0 /bin/ps -axo "
                "pid=,ppid=,pgid=,uid=,command=\n"
            ),
            "",
        )
        with patch.object(
            probe_module.subprocess,
            "Popen",
            return_value=sampler,
        ) as popen:
            rows = probe_module._read_process_table(
                fail_on_malformed=True
            )

        self.assertEqual(
            rows,
            [
                {
                    "pid": 1,
                    "ppid": 0,
                    "pgid": 1,
                    "uid": 0,
                    "command": "/sbin/launchd",
                },
                {
                    "pid": 9000,
                    "ppid": 1,
                    "pgid": 9000,
                    "uid": 501,
                    "command": "python replay-operator",
                },
            ],
        )
        popen.assert_called_once_with(
            [
                "/bin/ps",
                "-axo",
                "pid=,ppid=,pgid=,uid=,command=",
            ],
            stdout=probe_module.subprocess.PIPE,
            stderr=probe_module.subprocess.PIPE,
            text=True,
        )
        sampler.communicate.assert_called_once_with()

    def test_process_table_sampling_failures_are_sanitized(
        self,
    ) -> None:
        from scheduler import daily_control_plane_probe as probe_module

        valid_stdout = (
            "1 0 1 0 /sbin/launchd\n"
            "9000 1 9000 501 python replay-operator\n"
            "7300 9000 9000 0 /bin/ps -axo "
            "pid=,ppid=,pgid=,uid=,command=\n"
        )
        scenarios = (
            {
                "pid": 7300,
                "returncode": 1,
                "stdout": valid_stdout,
                "stderr": "--secret failure",
            },
            {
                "pid": 7300,
                "returncode": 0,
                "stdout": "",
                "stderr": "",
            },
            {
                "pid": 7300,
                "returncode": 0,
                "stdout": valid_stdout,
                "stderr": "--secret warning",
            },
            {
                "pid": 7300,
                "returncode": 0,
                "stdout": (
                    "1 0 1 0 /sbin/launchd\n"
                    "9000 1 9000 501 python replay-operator\n"
                ),
                "stderr": "",
            },
        )
        for scenario in scenarios:
            sampler = MagicMock(
                pid=scenario["pid"],
                returncode=scenario["returncode"],
            )
            sampler.communicate.return_value = (
                scenario["stdout"],
                scenario["stderr"],
            )
            with self.subTest(scenario=scenario):
                with patch.object(
                    probe_module.subprocess,
                    "Popen",
                    return_value=sampler,
                ):
                    try:
                        probe_module._read_process_table(
                            fail_on_malformed=True
                        )
                    except Exception as exc:
                        raised = exc
                    else:
                        self.fail(
                            "unsafe process table sample was accepted"
                        )
                self.assertIsInstance(raised, RuntimeError)
                self.assertEqual(
                    str(raised),
                    "OS process table sampling failed",
                )
                self.assertNotIn("--secret", str(raised))

    def test_fails_closed_on_unsafe_registered_process_identity(
        self,
    ) -> None:
        unsafe = SimpleNamespace(
            process_id=4200,
            process_group_id=4201,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "registered replay process identity",
        ):
            self._probe(registered_processes=(unsafe,))

    def test_replay_parse_fails_closed_without_exposing_bad_line(
        self,
    ) -> None:
        from scheduler import daily_control_plane_probe as probe_module

        sampler = MagicMock(pid=7300, returncode=0)
        sampler.communicate.return_value = (
            (
                "4200 malformed process row "
                "python -m scheduler.scheme_runner --secret value\n"
            ),
            "",
        )
        with (
            patch(
                "scheduler.repository."
                "read_current_replay_attempt_processes",
                return_value=(),
            ),
            patch.object(
                probe_module.subprocess,
                "Popen",
                return_value=sampler,
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "^OS process table is malformed$",
            ) as raised:
                probe_module.probe_isolated_replay_process_boundary(
                    object(),
                    service_uid=501,
                    occurrence_id=31,
                    active_item_ids=(),
                )

        self.assertNotIn("--secret", str(raised.exception))


class ProductionQuiescenceCompatibilityTests(unittest.TestCase):
    def test_replay_probe_excludes_only_backend_processes(self) -> None:
        from scheduler import daily_control_plane_probe as probe_module

        engine = SimpleNamespace(dispose=Mock())
        database_report = {
            "active_occurrence_count": 0,
            "nonterminal_item_count": 0,
            "running_ledger_run_count": 0,
            "cleanup_pending_count": 0,
            "running_legacy_scheduled_live_run_count": 0,
        }
        processes = (
            {
                "pid": 4100,
                "ppid": 1,
                "pgid": 4100,
                "uid": 501,
                "command": "python -m backend.main",
            },
            {
                "pid": 4200,
                "ppid": 1,
                "pgid": 4200,
                "uid": 501,
                "command": "python -m scheduler.scheme_runner",
            },
        )
        with (
            patch(
                "scheduler.repository.create_engine_from_env",
                return_value=engine,
            ),
            patch.object(
                probe_module,
                "_read_database_quiescence",
                return_value=(database_report, ()),
            ),
            patch.object(
                probe_module,
                "_read_process_table",
                return_value=list(processes),
            ),
            patch.object(probe_module.os, "getpid", return_value=9000),
        ):
            production_report = (
                probe_module.probe_daily_transition_quiescence(
                    501,
                    date(2026, 7, 27),
                )
            )
            replay_report = probe_module.probe_daily_transition_quiescence(
                501,
                date(2026, 7, 27),
                allow_backend=True,
            )

        self.assertEqual(production_report["project_process_count"], 2)
        self.assertEqual(replay_report["project_process_count"], 1)
        self.assertEqual(engine.dispose.call_count, 2)

    def test_production_probe_keeps_global_zero_tolerance_counts(
        self,
    ) -> None:
        from scheduler import daily_control_plane_probe as probe_module

        engine = SimpleNamespace(dispose=Mock())
        database_report = {
            "active_occurrence_count": 0,
            "nonterminal_item_count": 0,
            "running_ledger_run_count": 0,
            "cleanup_pending_count": 0,
            "running_legacy_scheduled_live_run_count": 0,
        }
        registered_rows = (
            {
                "process_id": 4200,
                "process_group_id": 4200,
            },
        )
        processes = (
            {
                "pid": 4200,
                "ppid": 1,
                "pgid": 4200,
                "uid": 501,
                "command": "python -m scheduler.scheme_runner",
            },
            {
                "pid": 4300,
                "ppid": 1,
                "pgid": 4300,
                "uid": 501,
                "command": "python -m scheduler.scheme_runner",
            },
        )
        with (
            patch(
                "scheduler.repository.create_engine_from_env",
                return_value=engine,
            ),
            patch.object(
                probe_module,
                "_read_database_quiescence",
                return_value=(database_report, registered_rows),
            ),
            patch.object(
                probe_module,
                "_read_process_table",
                return_value=list(processes),
            ),
            patch.object(probe_module.os, "getpid", return_value=9000),
        ):
            report = probe_module.probe_daily_transition_quiescence(
                501,
                date(2026, 7, 27),
            )

        self.assertEqual(report["registered_process_alive_count"], 1)
        self.assertEqual(report["project_process_count"], 2)
        engine.dispose.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
