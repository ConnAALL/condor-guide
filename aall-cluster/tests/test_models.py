from __future__ import annotations

import unittest

from aall_cluster.models import Job, build_user_stats, group_jobs, workers_from_ads


class JobTests(unittest.TestCase):
    def test_job_from_ad_maps_status_resources_and_host(self) -> None:
        job = Job.from_ad(
            {
                "ClusterId": 14145,
                "ProcId": 3,
                "Owner": "mega_knight",
                "JobStatus": 2,
                "Cmd": "/tmp/run_scan.sh",
                "RemoteHost": "slot1_1@dsr11",
                "RequestCpus": 8,
                "RequestMemory": 10240,
                "RemoteUserCpu": 100.5,
                "RemoteSysCpu": 2.5,
            }
        )

        self.assertEqual(job.job_id, "14145.3")
        self.assertEqual(job.state, "RUNNING")
        self.assertEqual(job.host, "dsr11")
        self.assertEqual(job.name, "run_scan.sh")
        self.assertEqual(job.cpu_seconds, 103.0)

    def test_condor_idle_is_presented_as_pending_and_active(self) -> None:
        job = Job.from_ad(
            {"ClusterId": 9, "ProcId": 0, "Owner": "a", "JobStatus": 1}
        )

        self.assertEqual(job.state, "PENDING")
        self.assertTrue(job.active)

    def test_job_groups_preserve_cluster_tasks(self) -> None:
        jobs = [
            Job.from_ad({"ClusterId": 10, "ProcId": 10, "Owner": "a", "JobStatus": 1}),
            Job.from_ad({"ClusterId": 10, "ProcId": 2, "Owner": "a", "JobStatus": 2}),
            Job.from_ad({"ClusterId": 11, "ProcId": 0, "Owner": "b", "JobStatus": 5}),
        ]

        groups = group_jobs(jobs)

        self.assertEqual([group.job_id for group in groups], ["10.*", "11.*"])
        self.assertEqual(groups[0].total, 2)
        self.assertEqual(groups[0].count("RUNNING"), 1)
        self.assertEqual(groups[0].dominant_state, "RUNNING")


class WorkerTests(unittest.TestCase):
    def test_partitionable_and_dynamic_slots_become_one_worker(self) -> None:
        ads = [
            {
                "Machine": "dsr11",
                "Name": "slot1@dsr11",
                "MyAddress": "<136.244.224.20:9618?addrs=136.244.224.20-9618&alias=bonfire>",
                "State": "Unclaimed",
                "Activity": "Idle",
                "PartitionableSlot": True,
                "Cpus": 0,
                "Memory": 3661,
                "Gpus": 0,
                "TotalSlotCpus": 8,
                "TotalSlotMemory": 13901,
                "TotalSlotGpus": 0,
                "AALL_TIER": "server",
                "LastHeardFrom": 100,
            },
            {
                "Machine": "dsr11",
                "Name": "slot1_1@dsr11",
                "State": "Claimed",
                "Activity": "Busy",
                "DynamicSlot": True,
                "Cpus": 8,
                "Memory": 10240,
                "RemoteOwner": "mega_knight@aall.conncoll.edu",
                "JobId": "14145.3",
                "LoadAvg": 7.99,
                "LastHeardFrom": 101,
            },
        ]

        workers = workers_from_ads(ads)

        self.assertEqual(len(workers), 1)
        worker = workers[0]
        self.assertEqual(worker.total_cpus, 8)
        self.assertEqual(worker.used_cpus, 8)
        self.assertEqual(worker.used_memory_mb, 10240)
        self.assertEqual(worker.ip_address, "136.244.224.20")
        self.assertTrue(worker.busy)
        self.assertEqual(worker.owners, ("mega_knight",))
        self.assertEqual(worker.job_ids, ("14145.3",))

    def test_idle_partitionable_slot_reports_all_resources_free(self) -> None:
        worker = workers_from_ads(
            [
                {
                    "Machine": "bonfire",
                    "State": "Unclaimed",
                    "Activity": "Idle",
                    "PartitionableSlot": True,
                    "Cpus": 16,
                    "Memory": 15708,
                    "Gpus": 1,
                    "TotalSlotCpus": 16,
                    "TotalSlotMemory": 15708,
                    "TotalSlotGpus": 1,
                }
            ]
        )[0]

        self.assertEqual(worker.used_cpus, 0)
        self.assertEqual(worker.free_gpus, 1)
        self.assertFalse(worker.busy)


class UserStatsTests(unittest.TestCase):
    def test_user_stats_include_queue_only_and_accounting_only_users(self) -> None:
        from aall_cluster.models import PriorityRecord

        jobs = [
            Job.from_ad({"ClusterId": 1, "ProcId": 0, "Owner": "aall", "JobStatus": 5, "RequestCpus": 2}),
        ]
        priorities = [
            PriorityRecord("mega@domain", 10.0, 1000.0, 8.0, 3600.0, 1, 2)
        ]

        users = {entry.user: entry for entry in build_user_stats(jobs, priorities)}

        self.assertEqual(users["aall"].held, 1)
        self.assertEqual(users["aall"].requested_cpus, 2)
        self.assertEqual(users["mega"].resources_used, 8.0)


if __name__ == "__main__":
    unittest.main()
