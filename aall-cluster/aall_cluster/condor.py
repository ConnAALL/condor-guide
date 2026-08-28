from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from .models import Job, PriorityRecord, Worker, workers_from_ads


class CondorError(RuntimeError):
    pass


Runner = Callable[[Sequence[str]], str]


JOB_ATTRIBUTES = (
    "ClusterId,ProcId,Owner,JobStatus,JobUniverse,Cmd,Args,JobBatchName,QDate,"
    "EnteredCurrentStatus,RemoteHost,RequestCpus,RequestMemory,RequestGpus,"
    "MemoryUsage,RemoteUserCpu,RemoteSysCpu,DiskUsage,JobPrio,HoldReason,"
    "LastRejMatchReason,LastMatchReason,NumJobStarts,CompletionDate,ExitCode,"
    "ExitBySignal"
)

WORKER_ATTRIBUTES = (
    "Machine,Name,MyAddress,State,Activity,Cpus,Memory,Gpus,TotalSlotCpus,TotalSlotMemory,"
    "TotalSlotGpus,TotalCpus,TotalMemory,TotalGpus,LoadAvg,RemoteOwner,AALL_TIER,"
    "OpSysAndVer,Arch,EnteredCurrentActivity,LastHeardFrom,SlotType,"
    "PartitionableSlot,DynamicSlot,ClientMachine,JobId"
)

SCHEDD_ATTRIBUTES = (
    "Name,Machine,TotalRunningJobs,TotalIdleJobs,TotalHeldJobs,TotalJobAds,"
    "MaxJobsRunning,RecentDaemonCoreDutyCycle,LastHeardFrom"
)


@dataclass(frozen=True)
class ClusterSnapshot:
    jobs: tuple[Job, ...]
    workers: tuple[Worker, ...]
    priorities: tuple[PriorityRecord, ...]
    schedd: dict[str, object]
    condor_version: str
    fetched_at: float


class CondorClient:
    def __init__(self, runner: Runner | None = None):
        self._runner = runner or self._subprocess_runner
        self._version: str | None = None

    @staticmethod
    def _subprocess_runner(command: Sequence[str]) -> str:
        try:
            result = subprocess.run(
                list(command),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise CondorError(
                f"{command[0]} was not found; run aall-cluster on mega-knight"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CondorError(f"{' '.join(command)} timed out") from exc
        if result.returncode:
            message = result.stderr.strip() or result.stdout.strip()
            raise CondorError(message or f"{' '.join(command)} failed")
        return result.stdout

    def _json(self, command: Sequence[str]) -> list[dict[str, object]]:
        output = self._runner(command)
        try:
            value = json.loads(output or "[]")
        except json.JSONDecodeError as exc:
            raise CondorError(f"invalid JSON from {command[0]}: {exc}") from exc
        if not isinstance(value, list):
            raise CondorError(f"unexpected output from {command[0]}")
        return [item for item in value if isinstance(item, dict)]

    def fetch_jobs(self) -> list[Job]:
        ads = self._json(
            ["condor_q", "-allusers", "-json", "-attributes", JOB_ATTRIBUTES]
        )
        return [Job.from_ad(ad) for ad in ads]

    def fetch_workers(self) -> list[Worker]:
        ads = self._json(
            ["condor_status", "-json", "-attributes", WORKER_ATTRIBUTES]
        )
        return workers_from_ads(ads)

    def fetch_history(
        self,
        limit: int | None = 500,
        *,
        since_epoch: int | None = None,
    ) -> list[Job]:
        command = ["condor_history"]
        if since_epoch is not None:
            command.extend(
                ["-constraint", f"CompletionDate >= {max(0, int(since_epoch))}"]
            )
        elif limit is not None:
            command.extend(["-limit", str(max(1, limit))])
        command.extend(["-json", "-attributes", JOB_ATTRIBUTES])
        ads = self._json(command)
        return [Job.from_ad(ad) for ad in ads]

    def fetch_job_activity(
        self,
        window_seconds: int,
        *,
        now: float | None = None,
    ) -> list[Job]:
        """Return active jobs plus terminal jobs completed in the time window."""
        cutoff = int(time.time() if now is None else now) - max(
            0, int(window_seconds)
        )
        history = self.fetch_history(limit=None, since_epoch=cutoff)
        by_id = {job.job_id: job for job in history}
        # Queue ClassAds are authoritative if a job also appears in history
        # during an HTCondor state transition.
        for job in self.fetch_jobs():
            by_id[job.job_id] = job
        return list(by_id.values())

    def fetch_priorities(self) -> list[PriorityRecord]:
        fields = [
            "Name",
            "IsAccountingGroup",
            "Priority",
            "PriorityFactor",
            "WeightedResourcesUsed",
            "WeightedAccumulatedUsage",
            "BeginUsageTime",
            "LastUsageTime",
        ]
        output = self._runner(
            ["condor_userprio", "-all", "-allusers", "-af:t", *fields]
        )
        records = []
        for line in output.splitlines():
            values = line.split("\t")
            if len(values) != len(fields):
                continue
            name, is_group, priority, factor, used, usage, begin, last = values
            if is_group.strip().lower() == "true" or name == "<none>":
                continue
            try:
                records.append(
                    PriorityRecord(
                        name=name,
                        effective_priority=float(priority),
                        priority_factor=float(factor),
                        resources_used=float(used),
                        weighted_usage_seconds=float(usage),
                        usage_started_at=int(float(begin)),
                        last_used_at=int(float(last)),
                    )
                )
            except ValueError:
                continue
        return records

    def fetch_schedd(self) -> dict[str, object]:
        ads = self._json(
            ["condor_status", "-schedd", "-json", "-attributes", SCHEDD_ATTRIBUTES]
        )
        return ads[0] if ads else {}

    def version(self) -> str:
        if self._version is None:
            first_line = self._runner(["condor_version"]).splitlines()[0]
            self._version = first_line.strip("$ ")
        return self._version

    def snapshot(self) -> ClusterSnapshot:
        return ClusterSnapshot(
            jobs=tuple(self.fetch_jobs()),
            workers=tuple(self.fetch_workers()),
            priorities=tuple(self.fetch_priorities()),
            schedd=self.fetch_schedd(),
            condor_version=self.version(),
            fetched_at=time.time(),
        )
