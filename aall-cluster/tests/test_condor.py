from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from aall_cluster.__main__ import (
    SNAPSHOT_VIEWS,
    TUI_VIEWS,
    _snapshot,
    build_parser,
    remote_command,
)
from aall_cluster.condor import CondorClient, CondorError, WORKER_ATTRIBUTES
from aall_cluster.models import Job


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> str:
        command = list(command)
        self.commands.append(command)
        if command[0] == "condor_q":
            return json.dumps(
                [{"ClusterId": 7, "ProcId": 2, "Owner": "aall", "JobStatus": 1}]
            )
        if command[:2] == ["condor_status", "-json"]:
            return json.dumps(
                [
                    {
                        "Machine": "worker1",
                        "State": "Unclaimed",
                        "Activity": "Idle",
                        "PartitionableSlot": True,
                        "Cpus": 4,
                        "Memory": 8000,
                        "TotalSlotCpus": 4,
                        "TotalSlotMemory": 8000,
                    }
                ]
            )
        if command[:2] == ["condor_status", "-schedd"]:
            return '[{"Name":"manager","TotalJobAds":1}]'
        if command[0] == "condor_userprio":
            return (
                "<none>\ttrue\t1\t0\t1\t3600\t1\t2\n"
                "aall@domain\tfalse\t500\t1000\t0\t7200\t1\t2\n"
            )
        if command[0] == "condor_version":
            return "$CondorVersion: 25.8.2 test $\n$CondorPlatform: X86_64 $\n"
        if command[0] == "condor_history":
            return "[]"
        raise AssertionError(command)


class CondorClientTests(unittest.TestCase):
    def test_snapshot_fetches_and_parses_every_live_source(self) -> None:
        runner = FakeRunner()
        snapshot = CondorClient(runner=runner).snapshot()

        self.assertEqual(snapshot.jobs[0].job_id, "7.2")
        self.assertEqual(snapshot.workers[0].machine, "worker1")
        self.assertEqual(snapshot.priorities[0].user, "aall")
        self.assertEqual(snapshot.schedd["Name"], "manager")
        self.assertIn("CondorVersion: 25.8.2", snapshot.condor_version)

    def test_priority_parser_skips_group_rollup(self) -> None:
        records = CondorClient(runner=FakeRunner()).fetch_priorities()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].effective_priority, 500.0)
        self.assertEqual(records[0].weighted_usage_seconds, 7200.0)

    def test_invalid_json_is_reported_as_condor_error(self) -> None:
        client = CondorClient(runner=lambda _command: "not json")

        with self.assertRaisesRegex(CondorError, "invalid JSON"):
            client.fetch_jobs()

    def test_job_activity_merges_time_bounded_history_with_live_queue(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> str:
            command = list(command)
            commands.append(command)
            if command[0] == "condor_history":
                return json.dumps(
                    [
                        {
                            "ClusterId": 7,
                            "ProcId": 0,
                            "Owner": "aall",
                            "JobStatus": 4,
                            "CompletionDate": 9_900,
                        },
                        {
                            "ClusterId": 8,
                            "ProcId": 0,
                            "Owner": "aall",
                            "JobStatus": 4,
                            "CompletionDate": 9_950,
                        },
                    ]
                )
            if command[0] == "condor_q":
                return json.dumps(
                    [
                        {
                            "ClusterId": 7,
                            "ProcId": 0,
                            "Owner": "aall",
                            "JobStatus": 2,
                        }
                    ]
                )
            raise AssertionError(command)

        jobs = CondorClient(runner=runner).fetch_job_activity(3600, now=10_000)

        by_id = {job.job_id: job for job in jobs}
        self.assertEqual(by_id["7.0"].state, "RUNNING")
        self.assertEqual(by_id["8.0"].state, "COMPLETED")
        history_command = commands[0]
        self.assertIn("-constraint", history_command)
        self.assertIn("CompletionDate >= 6400", history_command)
        self.assertNotIn("-limit", history_command)

    def test_cli_exposes_jobs_workers_and_history_views(self) -> None:
        self.assertEqual(TUI_VIEWS, ("jobs", "workers", "history"))
        self.assertEqual(SNAPSHOT_VIEWS, ("jobs", "workers", "history"))
        self.assertEqual(build_parser().parse_args(["--view", "history"]).view, "history")

    def test_worker_query_requests_node_address(self) -> None:
        self.assertIn("MyAddress", WORKER_ATTRIBUTES)

    def test_job_snapshot_filters_terminal_queue_entries(self) -> None:
        class SnapshotClient:
            def fetch_jobs(self):
                return [
                    Job.from_ad({"ClusterId": 1, "ProcId": 0, "JobStatus": 2}),
                    Job.from_ad({"ClusterId": 2, "ProcId": 0, "JobStatus": 5}),
                ]

        text = _snapshot(SnapshotClient(), "jobs")

        self.assertIn("1.0", text)
        self.assertNotIn("2.0", text)


class RemoteCommandTests(unittest.TestCase):
    def test_tui_allocates_a_remote_terminal(self) -> None:
        with patch.dict(os.environ, {"AALL_CLUSTER_KEY": "/does/not/exist"}):
            command = remote_command(["--view", "workers"])

        self.assertEqual(command[:2], ["ssh", "-t"])
        self.assertEqual(
            command[-3:],
            ["/home/aall/.local/bin/aall-cluster", "--view", "workers"],
        )

    def test_snapshot_does_not_force_a_remote_terminal(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AALL_CLUSTER_KEY": "/does/not/exist",
                "AALL_CLUSTER_HOST": "user@example",
            },
        ):
            command = remote_command(["--snapshot", "jobs"])

        self.assertNotIn("-t", command)
        self.assertIn("user@example", command)


if __name__ == "__main__":
    unittest.main()
