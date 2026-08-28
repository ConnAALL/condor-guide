from __future__ import annotations

import time
import unittest

from aall_cluster.condor import ClusterSnapshot
from aall_cluster.formatting import duration, memory, natural_key
from aall_cluster.models import Job, PriorityRecord, workers_from_ads
from aall_cluster.ui import (
    AallClusterApp,
    JOB_WINDOWS,
    TABS,
    popup_geometry,
    snapshot_text,
)


class FakeWindow:
    def __init__(self, height: int, width: int) -> None:
        self.height = height
        self.width = width
        self.cells = [[" " for _ in range(width)] for _ in range(height)]
        self.writes: list[tuple[int, int, str, int]] = []

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def addstr(self, y: int, x: int, text: str, _attr: int = 0) -> None:
        self.writes.append((y, x, text, _attr))
        for offset, character in enumerate(text):
            self.cells[y][x + offset] = character

    def row(self, y: int) -> str:
        return "".join(self.cells[y])


class FormattingTests(unittest.TestCase):
    def test_natural_job_id_order(self) -> None:
        values = ["100.10", "99.4", "100.2"]
        self.assertEqual(sorted(values, key=natural_key), ["99.4", "100.2", "100.10"])

    def test_duration_and_memory(self) -> None:
        self.assertEqual(duration(3661), "1:01:01")
        self.assertEqual(duration(90061), "1d01h")
        self.assertEqual(memory(1536), "1.5G")


class SnapshotTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.job = Job.from_ad(
            {
                "ClusterId": 12,
                "ProcId": 0,
                "Owner": "alice",
                "JobStatus": 2,
                "RemoteHost": "slot1@worker1",
                "RequestCpus": 2,
                "RequestMemory": 2048,
            }
        )
        self.worker = workers_from_ads(
            [
                {
                    "Machine": "worker1",
                    "MyAddress": "<136.244.224.20:9618?addrs=136.244.224.20-9618>",
                    "State": "Unclaimed",
                    "Activity": "Idle",
                    "PartitionableSlot": True,
                    "Cpus": 2,
                    "Memory": 6000,
                    "TotalSlotCpus": 4,
                    "TotalSlotMemory": 8000,
                },
                {
                    "Machine": "worker1",
                    "State": "Claimed",
                    "Activity": "Busy",
                    "DynamicSlot": True,
                    "RemoteOwner": "alice@domain",
                    "JobId": "12.0",
                },
            ]
        )[0]
        self.priority = PriorityRecord("alice@domain", 100.0, 1000.0, 2.0, 7200.0, 1, 2)
        self.snapshot = ClusterSnapshot(
            jobs=(self.job,),
            workers=(self.worker,),
            priorities=(self.priority,),
            schedd={"Machine": "manager"},
            condor_version="CondorVersion: test",
            fetched_at=time.time(),
        )

    def test_job_snapshot_uses_pending_vocabulary(self) -> None:
        pending = Job.from_ad(
            {"ClusterId": 13, "ProcId": 0, "Owner": "alice", "JobStatus": 1}
        )
        snapshot = ClusterSnapshot(
            jobs=(pending,),
            workers=(),
            priorities=(),
            schedd={},
            condor_version="",
            fetched_at=time.time(),
        )
        text = snapshot_text(snapshot, "jobs")

        self.assertIn("PENDING", text)
        self.assertNotIn("IDLE", text)

    def test_worker_snapshot_does_not_list_dynamic_slot_as_second_worker(self) -> None:
        text = snapshot_text(self.snapshot, "workers")

        self.assertIn("1 connected workers", text)
        self.assertIn("CPUs 2/4 available", text)
        self.assertIn("GPUs 0/0 available", text)
        self.assertEqual(text.count("worker1"), 1)
        self.assertIn("136.244.224.20", text)
        self.assertNotIn("STATE", text)
        self.assertNotIn("ACTIVITY", text)
        self.assertNotIn("alice", text)


class TuiTests(unittest.TestCase):
    def test_jobs_workers_and_history_tabs_are_present(self) -> None:
        self.assertEqual(
            TABS,
            (
                ("jobs", "j", "Jobs"),
                ("workers", "n", "Workers"),
                ("history", "h", "History"),
            ),
        )

    def test_history_uses_vaccs_time_windows(self) -> None:
        self.assertEqual(
            [label for _seconds, label in JOB_WINDOWS],
            ["1h", "3h", "24h", "3d", "7d"],
        )

    def test_history_header_lists_time_windows(self) -> None:
        app = AallClusterApp(object())
        app.state.view = "history"
        app._pair = lambda pair_id: pair_id
        screen = FakeWindow(8, 100)

        app._draw_header(screen, screen.width)

        labels = {text: attr for _y, _x, text, attr in screen.writes}
        for label in ("1h", "3h", "24h", "3d", "7d"):
            self.assertIn(label, labels)

    def test_jobs_show_only_running_and_pending_queue_entries(self) -> None:
        app = AallClusterApp(object())
        app.state.jobs = [
            Job.from_ad(
                {
                    "ClusterId": 1,
                    "ProcId": 0,
                    "Owner": "alice",
                    "JobStatus": 2,
                    "QDate": 1,
                }
            ),
            Job.from_ad(
                {
                    "ClusterId": 2,
                    "ProcId": 0,
                    "Owner": "alice",
                    "JobStatus": 1,
                    "QDate": 1,
                }
            ),
            Job.from_ad(
                {
                    "ClusterId": 3,
                    "ProcId": 0,
                    "Owner": "alice",
                    "JobStatus": 4,
                    "CompletionDate": 100,
                }
            ),
            Job.from_ad(
                {
                    "ClusterId": 4,
                    "ProcId": 0,
                    "Owner": "alice",
                    "JobStatus": 5,
                }
            ),
        ]

        self.assertEqual(
            {job.job_id for job in app._visible_jobs()},
            {"1.0", "2.0"},
        )
        self.assertEqual(
            {job.state for job in app._visible_jobs()},
            {"RUNNING", "PENDING"},
        )

    def test_jobs_and_history_have_no_detail_action_or_panel(self) -> None:
        for view in ("jobs", "history"):
            with self.subTest(view=view):
                app = AallClusterApp(object(), initial_view=view)
                screen = FakeWindow(24, 100)

                app._draw_header(screen, screen.width)
                if view == "jobs":
                    app._draw_jobs(screen, screen.height, screen.width)
                else:
                    app._draw_history(screen, screen.height, screen.width)

                written = "\n".join(screen.row(y) for y in range(screen.height))
                self.assertNotIn("detail", written)
                self.assertNotIn("selected job", written)
                self.assertNotIn("selected history", written)
                self.assertEqual(screen.row(screen.height - 1)[0], "╰")
                self.assertTrue(app._handle_key(screen, ord("d")))

    def test_workers_header_has_only_gpu_filter_and_peek_actions(self) -> None:
        app = AallClusterApp(object(), initial_view="workers")
        screen = FakeWindow(8, 100)

        app._draw_header(screen, screen.width)

        written = "".join(text for _y, _x, text, _attr in screen.writes)
        self.assertIn(" g gpu-workers ", written)
        self.assertIn(" p peek ", written)
        for removed in ("free-gpu", "activity", "detail"):
            self.assertNotIn(removed, written)

    def test_workers_are_sorted_by_name(self) -> None:
        app = AallClusterApp(object())
        app.state.workers = workers_from_ads(
            [
                {"Machine": name, "State": "Unclaimed", "Activity": "Idle"}
                for name in ("worker10", "Worker2", "worker1")
            ]
        )

        self.assertEqual(
            [worker.machine for worker in app._visible_workers()],
            ["worker1", "Worker2", "worker10"],
        )

    def test_box_draws_the_right_border(self) -> None:
        app = AallClusterApp(object())
        window = FakeWindow(4, 8)

        app._draw_box(window, 0, 0, 4, 8)

        self.assertEqual([row[-1] for row in window.cells], ["╮", "│", "│", "╯"])

    def test_workers_render_resource_bars_at_matching_columns(self) -> None:
        app = AallClusterApp(object())
        app.state.view = "workers"
        app.state.workers = workers_from_ads(
            [
                {
                    "Machine": "worker1",
                    "MyAddress": "<136.244.224.20:9618?alias=bonfire>",
                    "State": "Unclaimed",
                    "Activity": "Idle",
                    "PartitionableSlot": True,
                    "Cpus": 4,
                    "Memory": 8000,
                    "Gpus": 1,
                    "TotalSlotCpus": 4,
                    "TotalSlotMemory": 8000,
                    "TotalSlotGpus": 1,
                },
                {
                    "Machine": "worker200",
                    "MyAddress": "<136.244.224.21:9618?alias=worker200>",
                    "State": "Unclaimed",
                    "Activity": "Idle",
                    "PartitionableSlot": True,
                    "Cpus": 120,
                    "Memory": 128000,
                    "Gpus": 8,
                    "TotalSlotCpus": 128,
                    "TotalSlotMemory": 256000,
                    "TotalSlotGpus": 8,
                },
            ]
        )
        screen = FakeWindow(24, 180)

        app._draw_workers(screen, screen.height, screen.width)

        first_bars = [index for index, char in enumerate(screen.row(7)) if char == "["]
        second_bars = [index for index, char in enumerate(screen.row(8)) if char == "["]
        self.assertEqual(first_bars, second_bars)
        self.assertEqual(len(first_bars), 3)
        header = screen.row(6)
        title = screen.row(5)
        self.assertIn("CPUs 124/132 available", title)
        self.assertIn("GPUs 9/9 available", title)
        self.assertIn("IP", header)
        self.assertNotIn("STATE", header)
        self.assertNotIn("OWNER", header)
        self.assertNotIn("selected worker", "\n".join(screen.row(y) for y in range(screen.height)))

    def test_popup_geometry_tracks_short_and_long_content(self) -> None:
        short = popup_geometry(40, 120, " peek ", "one line")
        long = popup_geometry(40, 120, " peek ", "x" * 300)

        self.assertLess(short[2], long[2])
        self.assertLess(short[3], long[3])
        self.assertLess(short[2], 40 - 4)
        self.assertLess(short[3], 120 - 6)


if __name__ == "__main__":
    unittest.main()
