from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


JOB_STATES = {
    0: "UNEXPANDED",
    # HTCondor calls queued work IDLE.  Present it as PENDING so the Jobs
    # table uses the same vocabulary as vaccs-running.
    1: "PENDING",
    2: "RUNNING",
    3: "REMOVED",
    4: "COMPLETED",
    5: "HELD",
    6: "TRANSFERRING",
    7: "SUSPENDED",
}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def short_owner(owner: str) -> str:
    return owner.split("@", 1)[0]


def address_ip(address: Any) -> str:
    """Extract the node IP from an HTCondor ``MyAddress`` value."""
    value = str(address or "").strip().strip('"').strip("<>")
    endpoint = value.split("?", 1)[0]
    if endpoint.startswith("[") and "]" in endpoint:
        return endpoint[1 : endpoint.index("]")]
    if ":" in endpoint:
        return endpoint.rsplit(":", 1)[0]
    return endpoint or "-"


@dataclass(frozen=True)
class Job:
    cluster_id: int
    proc_id: int
    owner: str
    status_code: int
    command: str = ""
    arguments: str = ""
    batch_name: str = ""
    queued_at: int = 0
    status_since: int = 0
    remote_host: str = ""
    request_cpus: int = 1
    request_memory_mb: int = 0
    request_gpus: int = 0
    memory_usage_mb: int = 0
    remote_user_cpu: float = 0.0
    remote_sys_cpu: float = 0.0
    disk_usage_kb: int = 0
    priority: int = 0
    hold_reason: str = ""
    idle_reason: str = ""
    starts: int = 0
    completed_at: int = 0
    exit_code: int | None = None
    exit_by_signal: bool = False
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_ad(cls, ad: dict[str, Any]) -> "Job":
        exit_code = ad.get("ExitCode")
        return cls(
            cluster_id=_int(ad.get("ClusterId")),
            proc_id=_int(ad.get("ProcId")),
            owner=str(ad.get("Owner", "?")),
            status_code=_int(ad.get("JobStatus")),
            command=str(ad.get("Cmd", "")),
            arguments=str(ad.get("Args", "")),
            batch_name=str(ad.get("JobBatchName", "")),
            queued_at=_int(ad.get("QDate")),
            status_since=_int(ad.get("EnteredCurrentStatus")),
            remote_host=str(ad.get("RemoteHost", "")),
            request_cpus=max(0, _int(ad.get("RequestCpus"), 1)),
            request_memory_mb=max(0, _int(ad.get("RequestMemory"))),
            request_gpus=max(0, _int(ad.get("RequestGpus"))),
            memory_usage_mb=max(0, _int(ad.get("MemoryUsage"))),
            remote_user_cpu=max(0.0, _float(ad.get("RemoteUserCpu"))),
            remote_sys_cpu=max(0.0, _float(ad.get("RemoteSysCpu"))),
            disk_usage_kb=max(0, _int(ad.get("DiskUsage"))),
            priority=_int(ad.get("JobPrio")),
            hold_reason=str(ad.get("HoldReason", "")),
            idle_reason=str(
                ad.get("LastRejMatchReason", ad.get("LastMatchReason", ""))
            ),
            starts=max(0, _int(ad.get("NumJobStarts"))),
            completed_at=_int(ad.get("CompletionDate")),
            exit_code=None if exit_code is None else _int(exit_code),
            exit_by_signal=bool(ad.get("ExitBySignal", False)),
            raw=dict(ad),
        )

    @property
    def job_id(self) -> str:
        return f"{self.cluster_id}.{self.proc_id}"

    @property
    def owner_short(self) -> str:
        return short_owner(self.owner)

    @property
    def state(self) -> str:
        return JOB_STATES.get(self.status_code, f"STATUS-{self.status_code}")

    @property
    def name(self) -> str:
        if self.batch_name:
            return self.batch_name
        command = self.command.rstrip("/").rsplit("/", 1)[-1]
        return command or "-"

    @property
    def host(self) -> str:
        host = self.remote_host
        if "@" in host:
            host = host.split("@", 1)[1]
        return host or "-"

    @property
    def cpu_seconds(self) -> float:
        return self.remote_user_cpu + self.remote_sys_cpu

    @property
    def reason(self) -> str:
        if self.hold_reason:
            return self.hold_reason
        if self.idle_reason:
            return self.idle_reason
        return "-"

    @property
    def active(self) -> bool:
        return self.state in {"RUNNING", "PENDING"}


@dataclass(frozen=True)
class JobGroup:
    cluster_id: int
    owner: str
    name: str
    jobs: tuple[Job, ...]

    @property
    def job_id(self) -> str:
        return f"{self.cluster_id}.*"

    @property
    def total(self) -> int:
        return len(self.jobs)

    def count(self, state: str) -> int:
        return sum(job.state == state for job in self.jobs)

    @property
    def dominant_state(self) -> str:
        for state in ("RUNNING", "PENDING", "HELD", "TRANSFERRING", "COMPLETED"):
            if self.count(state):
                return state
        return self.jobs[0].state if self.jobs else "PENDING"

    @property
    def completed(self) -> int:
        return self.count("COMPLETED")

    @property
    def running(self) -> int:
        return self.count("RUNNING")

    @property
    def pending(self) -> int:
        return self.count("PENDING")

    @property
    def held(self) -> int:
        return self.count("HELD")

    @property
    def other(self) -> int:
        known = self.completed + self.running + self.pending + self.held
        return max(0, self.total - known)

    @property
    def queued_at(self) -> int:
        return min((job.queued_at for job in self.jobs if job.queued_at), default=0)

    @property
    def completed_at(self) -> int:
        return max((job.completed_at for job in self.jobs if job.completed_at), default=0)

    @property
    def cpus(self) -> int:
        return sum(job.request_cpus for job in self.jobs)

    @property
    def gpus(self) -> int:
        return sum(job.request_gpus for job in self.jobs)


def group_jobs(jobs: Iterable[Job]) -> list[JobGroup]:
    grouped: dict[tuple[int, str], list[Job]] = {}
    for job in jobs:
        grouped.setdefault((job.cluster_id, job.owner), []).append(job)
    groups = [
        JobGroup(cluster_id, owner, entries[0].name, tuple(entries))
        for (cluster_id, owner), entries in grouped.items()
    ]
    state_rank = {
        "RUNNING": 0,
        "PENDING": 1,
        "COMPLETED": 2,
        "HELD": 3,
    }
    return sorted(
        groups,
        key=lambda group: (
            state_rank.get(group.dominant_state, 4),
            -(group.completed_at or group.queued_at),
            -group.cluster_id,
            group.owner.casefold(),
        ),
    )


@dataclass(frozen=True)
class Worker:
    machine: str
    ip_address: str
    state: str
    activity: str
    total_cpus: int
    free_cpus: int
    total_memory_mb: int
    free_memory_mb: int
    total_gpus: int
    free_gpus: int
    load: float
    tier: str
    os_name: str
    arch: str
    last_heard_from: int
    activity_since: int
    owners: tuple[str, ...] = ()
    job_ids: tuple[str, ...] = ()
    slot_names: tuple[str, ...] = ()
    raw_ads: tuple[dict[str, Any], ...] = field(default_factory=tuple, compare=False, repr=False)

    @classmethod
    def from_ads(cls, machine: str, ads: list[dict[str, Any]]) -> "Worker":
        base = next(
            (ad for ad in ads if bool(ad.get("PartitionableSlot"))),
            ads[0],
        )
        dynamic = [ad for ad in ads if bool(ad.get("DynamicSlot"))]
        claimed = [ad for ad in dynamic if str(ad.get("State")) == "Claimed"]
        total_cpus = _int(base.get("TotalSlotCpus", base.get("TotalCpus", base.get("Cpus"))))
        total_memory = _int(
            base.get("TotalSlotMemory", base.get("TotalMemory", base.get("Memory")))
        )
        total_gpus = _int(base.get("TotalSlotGpus", base.get("TotalGpus", base.get("Gpus"))))
        partitionable = bool(base.get("PartitionableSlot"))
        free_cpus = _int(base.get("Cpus")) if partitionable else (
            total_cpus if str(base.get("State")) == "Unclaimed" else 0
        )
        free_memory = _int(base.get("Memory")) if partitionable else (
            total_memory if str(base.get("State")) == "Unclaimed" else 0
        )
        free_gpus = _int(base.get("Gpus")) if partitionable else (
            total_gpus if str(base.get("State")) == "Unclaimed" else 0
        )
        active_ads = claimed or dynamic or [base]
        activity = "Busy" if claimed else str(base.get("Activity", "Idle"))
        state = "Claimed" if claimed else str(base.get("State", "Unknown"))
        owners = tuple(
            sorted(
                {
                    short_owner(str(ad.get("RemoteOwner", "")))
                    for ad in claimed
                    if ad.get("RemoteOwner")
                }
            )
        )
        job_ids = tuple(
            sorted(str(ad.get("JobId")) for ad in claimed if ad.get("JobId"))
        )
        return cls(
            machine=machine,
            ip_address=address_ip(base.get("MyAddress")),
            state=state,
            activity=activity,
            total_cpus=max(0, total_cpus),
            free_cpus=max(0, free_cpus),
            total_memory_mb=max(0, total_memory),
            free_memory_mb=max(0, free_memory),
            total_gpus=max(0, total_gpus),
            free_gpus=max(0, free_gpus),
            load=max((_float(ad.get("LoadAvg")) for ad in active_ads), default=0.0),
            tier=str(base.get("AALL_TIER", "-")),
            os_name=str(base.get("OpSysAndVer", "-")),
            arch=str(base.get("Arch", "-")),
            last_heard_from=max((_int(ad.get("LastHeardFrom")) for ad in ads), default=0),
            activity_since=min(
                (_int(ad.get("EnteredCurrentActivity")) for ad in active_ads if ad.get("EnteredCurrentActivity")),
                default=0,
            ),
            owners=owners,
            job_ids=job_ids,
            slot_names=tuple(str(ad.get("Name", "")) for ad in ads),
            raw_ads=tuple(dict(ad) for ad in ads),
        )

    @property
    def used_cpus(self) -> int:
        return max(0, self.total_cpus - self.free_cpus)

    @property
    def used_memory_mb(self) -> int:
        return max(0, self.total_memory_mb - self.free_memory_mb)

    @property
    def used_gpus(self) -> int:
        return max(0, self.total_gpus - self.free_gpus)

    @property
    def busy(self) -> bool:
        return self.activity == "Busy" or self.state == "Claimed"


def workers_from_ads(ads: Iterable[dict[str, Any]]) -> list[Worker]:
    by_machine: dict[str, list[dict[str, Any]]] = {}
    for ad in ads:
        machine = str(ad.get("Machine", ad.get("Name", "unknown")))
        by_machine.setdefault(machine, []).append(ad)
    return [
        Worker.from_ads(machine, machine_ads)
        for machine, machine_ads in sorted(by_machine.items())
    ]


@dataclass(frozen=True)
class PriorityRecord:
    name: str
    effective_priority: float
    priority_factor: float
    resources_used: float
    weighted_usage_seconds: float
    usage_started_at: int
    last_used_at: int

    @property
    def user(self) -> str:
        return short_owner(self.name)


@dataclass(frozen=True)
class UserStats:
    user: str
    running: int = 0
    idle: int = 0
    held: int = 0
    other: int = 0
    requested_cpus: int = 0
    requested_gpus: int = 0
    resources_used: float = 0.0
    effective_priority: float | None = None
    weighted_usage_seconds: float = 0.0
    last_used_at: int = 0


def build_user_stats(
    jobs: Iterable[Job], priorities: Iterable[PriorityRecord]
) -> list[UserStats]:
    values: dict[str, dict[str, Any]] = {}
    for job in jobs:
        user = job.owner_short
        entry = values.setdefault(
            user,
            {"running": 0, "idle": 0, "held": 0, "other": 0, "cpus": 0, "gpus": 0},
        )
        bucket = "idle" if job.state == "PENDING" else job.state.lower()
        if bucket not in {"running", "idle", "held"}:
            bucket = "other"
        entry[bucket] += 1
        entry["cpus"] += job.request_cpus
        entry["gpus"] += job.request_gpus
    priority_by_user = {record.user: record for record in priorities}
    for user in priority_by_user:
        values.setdefault(
            user,
            {"running": 0, "idle": 0, "held": 0, "other": 0, "cpus": 0, "gpus": 0},
        )
    result = []
    for user, entry in values.items():
        priority = priority_by_user.get(user)
        result.append(
            UserStats(
                user=user,
                running=entry["running"],
                idle=entry["idle"],
                held=entry["held"],
                other=entry["other"],
                requested_cpus=entry["cpus"],
                requested_gpus=entry["gpus"],
                resources_used=priority.resources_used if priority else 0.0,
                effective_priority=priority.effective_priority if priority else None,
                weighted_usage_seconds=(priority.weighted_usage_seconds if priority else 0.0),
                last_used_at=priority.last_used_at if priority else 0,
            )
        )
    return result
