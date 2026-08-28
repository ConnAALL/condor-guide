from __future__ import annotations

import datetime as dt
import re
import time


def natural_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
    )


def duration(seconds: float | int) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def timestamp(epoch: int, empty: str = "-") -> str:
    if not epoch:
        return empty
    return dt.datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M")


def age(epoch: int, now: float | None = None) -> str:
    if not epoch:
        return "-"
    return duration((time.time() if now is None else now) - epoch)


def memory(mb: int) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.1f}G"
    return f"{mb}M"


def percent(used: int | float, total: int | float) -> float:
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, float(used) * 100.0 / float(total)))


def meter(used: int | float, total: int | float, width: int = 12) -> str:
    ratio = percent(used, total) / 100.0
    filled = round(ratio * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def job_elapsed(job: object, now: float | None = None) -> str:
    current = int(time.time() if now is None else now)
    state = getattr(job, "state", "")
    start = getattr(job, "status_since", 0) or getattr(job, "queued_at", 0)
    end = getattr(job, "completed_at", 0)
    if state in {"COMPLETED", "REMOVED"} and end:
        return duration(max(0, end - start))
    return duration(max(0, current - start))


def truncate(value: object, width: int) -> str:
    text = str(value)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"
