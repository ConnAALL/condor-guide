from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .condor import ClusterSnapshot, CondorClient, CondorError
from .ui import AallClusterApp, snapshot_text


TUI_VIEWS = ("jobs", "workers", "history")
SNAPSHOT_VIEWS = TUI_VIEWS


def remote_command(arguments: list[str]) -> list[str]:
    """Build the SSH command used when launched away from mega-knight."""
    command = ["ssh"]
    if (
        "--snapshot" not in arguments
        and "--help" not in arguments
        and "-h" not in arguments
        and "--version" not in arguments
    ):
        command.append("-t")
    key = Path(
        os.environ.get("AALL_CLUSTER_KEY", "~/.ssh/aall_id_ed25519")
    ).expanduser()
    if key.is_file():
        command.extend(["-i", str(key)])
    command.extend(
        [
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            os.environ.get("AALL_CLUSTER_HOST", "aall@136.244.224.136"),
            os.environ.get(
                "AALL_CLUSTER_REMOTE_COMMAND",
                "/home/aall/.local/bin/aall-cluster",
            ),
            *arguments,
        ]
    )
    return command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aall-cluster",
        description="Terminal UI for the AALL HTCondor pool.",
    )
    parser.add_argument(
        "-r",
        "--refresh",
        type=float,
        default=2.0,
        help="auto-refresh interval in seconds; 0 disables it (default: 2)",
    )
    parser.add_argument(
        "--view",
        choices=TUI_VIEWS,
        default="jobs",
        help="initial TUI view (default: jobs)",
    )
    parser.add_argument(
        "--snapshot",
        nargs="?",
        const="jobs",
        choices=SNAPSHOT_VIEWS,
        help="print one non-interactive snapshot instead of opening the TUI",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _snapshot(client: CondorClient, view: str) -> str:
    if view == "jobs":
        jobs = tuple(job for job in client.fetch_jobs() if job.active)
        snapshot = ClusterSnapshot(
            jobs=jobs,
            workers=(),
            priorities=(),
            schedd={},
            condor_version="",
            fetched_at=time.time(),
        )
        return snapshot_text(snapshot, view)
    if view == "history":
        jobs = tuple(client.fetch_job_activity(86400))
        snapshot = ClusterSnapshot(
            jobs=jobs,
            workers=(),
            priorities=(),
            schedd={},
            condor_version="",
            fetched_at=time.time(),
        )
        return snapshot_text(snapshot, view)
    if view == "workers":
        workers = tuple(client.fetch_workers())
        snapshot = ClusterSnapshot(
            jobs=(),
            workers=workers,
            priorities=(),
            schedd={},
            condor_version="",
            fetched_at=time.time(),
        )
        return snapshot_text(snapshot, view)
    raise AssertionError(view)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if shutil.which("condor_q") is None:
        command = remote_command(arguments)
        os.execvp(command[0], command)
    args = build_parser().parse_args(arguments)
    client = CondorClient()
    try:
        if args.snapshot:
            print(_snapshot(client, args.snapshot))
            return 0
        AallClusterApp(
            client,
            refresh_seconds=max(0.0, args.refresh),
            initial_view=args.view,
        ).run()
        return 0
    except KeyboardInterrupt:
        return 130
    except CondorError as exc:
        print(f"aall-cluster: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
