from __future__ import annotations

import curses
import textwrap
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .condor import ClusterSnapshot, CondorClient, CondorError
from .formatting import job_elapsed, memory, natural_key, percent, truncate
from .models import Job, JobGroup, Worker, group_jobs


MIN_WIDTH = 70
MIN_HEIGHT = 16

PAIR_GOOD = 1
PAIR_WARN = 2
PAIR_INFO = 3
PAIR_BAD = 4
PAIR_ACCENT = 5
PAIR_BORDER = 9
PAIR_TEXT = 10
PAIR_ACTIVE = 11
PAIR_TITLE = 12
PAIR_MUTED = 13

TABS = (
    ("jobs", "j", "Jobs"),
    ("workers", "n", "Workers"),
    ("history", "h", "History"),
)

JOB_WINDOWS = (
    (3600, "1h"),
    (3 * 3600, "3h"),
    (86400, "24h"),
    (3 * 86400, "3d"),
    (7 * 86400, "7d"),
)

THEME_256 = {
    "background": 16,
    "foreground": 255,
    "muted": 250,
    "green": 77,
    "yellow": 221,
    "cyan": 80,
    "red": 203,
    "orange": 173,
}

THEME_16 = {
    "background": curses.COLOR_BLACK,
    "foreground": curses.COLOR_WHITE,
    "muted": curses.COLOR_WHITE,
    "green": curses.COLOR_GREEN,
    "yellow": curses.COLOR_YELLOW,
    "cyan": curses.COLOR_CYAN,
    "red": curses.COLOR_RED,
    "orange": curses.COLOR_YELLOW,
}

THEME_PAIR_ROLES = {
    PAIR_GOOD: ("green", "background"),
    PAIR_WARN: ("yellow", "background"),
    PAIR_INFO: ("cyan", "background"),
    PAIR_BAD: ("red", "background"),
    PAIR_ACCENT: ("orange", "background"),
    PAIR_BORDER: ("orange", "background"),
    PAIR_TEXT: ("foreground", "background"),
    PAIR_ACTIVE: ("background", "orange"),
    PAIR_TITLE: ("foreground", "background"),
    PAIR_MUTED: ("muted", "background"),
}


@dataclass(frozen=True)
class Column:
    label: str
    minimum: int
    maximum: int
    value: Callable[[object], str]


@dataclass
class AppState:
    view: str = "jobs"
    jobs: list[Job] = field(default_factory=list)
    history: list[Job] = field(default_factory=list)
    workers: list[Worker] = field(default_factory=list)
    selected: int = 0
    scroll: int = 0
    message: str = ""
    last_refresh: float = 0.0
    gpu_workers_only: bool = False
    history_window_index: int = 2


def _job_color(state: str) -> int:
    return {
        "RUNNING": PAIR_GOOD,
        "PENDING": PAIR_WARN,
        "COMPLETED": PAIR_INFO,
        "TRANSFERRING": PAIR_INFO,
        "HELD": PAIR_BAD,
        "REMOVED": PAIR_BAD,
        "SUSPENDED": PAIR_BAD,
    }.get(state, PAIR_TEXT)


def _worker_color(worker: Worker) -> int:
    if worker.state in {"Owner", "Drained", "Preempting", "Delete"}:
        return PAIR_BAD
    if worker.busy:
        return PAIR_INFO
    if worker.state == "Unclaimed":
        return PAIR_GOOD
    return PAIR_WARN


def _state_counts(jobs: Sequence[Job]) -> str:
    order = ("RUNNING", "PENDING", "COMPLETED", "HELD", "REMOVED")
    counts = {state: sum(job.state == state for job in jobs) for state in order}
    parts = [f"{state}:{counts[state]}" for state in order if counts[state]]
    return " ".join(parts) if parts else "none"


def _bar(value: int | float, total: int | float, width: int) -> str:
    bounded = percent(value, total)
    filled = round(max(1, width) * bounded / 100.0)
    return "[" + "|" * filled + "." * (max(1, width) - filled) + "]"


def _resource_meter(
    used: int,
    total: int,
    *,
    meter_width: int,
    count_width: int,
) -> str:
    return f"{f'{used}/{total}'.rjust(count_width)} {_bar(used, total, meter_width)}"


def _resource_text_meter(
    text: str,
    used: int,
    total: int,
    *,
    meter_width: int,
    count_width: int,
) -> str:
    return f"{text.rjust(count_width)} {_bar(used, total, meter_width)}"


def _group_run_for(group: JobGroup) -> str:
    running = [job for job in group.jobs if job.state == "RUNNING"]
    if not running:
        return "-"
    return job_elapsed(
        min(
            running,
            key=lambda job: job.status_since or job.queued_at,
        )
    )


def _popup_lines(text: str, popup_width: int) -> list[str]:
    lines: list[str] = []
    for source_line in text.splitlines() or [""]:
        lines.extend(
            textwrap.wrap(
                source_line,
                max(1, popup_width - 4),
                replace_whitespace=False,
                drop_whitespace=False,
            )
            or [""]
        )
    return lines


def popup_geometry(
    screen_height: int,
    screen_width: int,
    title: str,
    text: str,
) -> tuple[int, int, int, int]:
    """Size a popup to its content, capped by the current terminal."""
    max_width = max(4, screen_width - 2)
    source_lines = text.splitlines() or [""]
    footer = " arrows scroll  q/esc close "
    desired_width = max(
        24,
        len(title) + 4,
        len(footer) + 4,
        max(len(line) for line in source_lines) + 4,
    )
    popup_width = min(max_width, desired_width)
    wrapped = _popup_lines(text, popup_width)
    max_height = max(4, screen_height - 2)
    popup_height = min(max_height, max(6, len(wrapped) + 4))
    top = max(0, (screen_height - popup_height) // 2)
    left = max(0, (screen_width - popup_width) // 2)
    return top, left, popup_height, popup_width


def snapshot_text(snapshot: ClusterSnapshot, view: str = "jobs") -> str:
    jobs = list(snapshot.jobs)
    workers = list(snapshot.workers)
    if view == "jobs":
        lines = ["JOB ID     OWNER          STATE         CPU   MEM  GPU  HOST / REASON"]
        active_jobs = [job for job in jobs if job.active]
        for job in sorted(active_jobs, key=lambda item: natural_key(item.job_id)):
            where = job.host if job.host != "-" else job.reason
            lines.append(
                f"{job.job_id:<10} {job.owner_short:<14} {job.state:<13} "
                f"{job.request_cpus:>3} {memory(job.request_memory_mb):>6} "
                f"{job.request_gpus:>3}  {where}"
            )
        return "\n".join(lines + [f"\n{len(active_jobs)} active jobs: {_state_counts(active_jobs)}"])
    if view == "history":
        groups = group_jobs(jobs)
        lines = ["JOBID           JOB                          REQ DONE RUN PEND HELD CPUS GPUS RUN_FOR"]
        for group in groups:
            lines.append(
                f"{group.job_id:<15} {truncate(group.name, 28):<28} "
                f"{group.total:>3} {group.completed:>4} {group.running:>3} "
                f"{group.pending:>4} {group.held:>4} {group.cpus:>4} "
                f"{group.gpus:>4} {_group_run_for(group):>7}"
            )
        return "\n".join(
            lines
            + [f"\n{len(groups)} history groups / {len(jobs)} tasks: {_state_counts(jobs)}"]
        )
    if view == "workers":
        lines = ["WORKER        IP               TIER          CPU       MEMORY        GPU"]
        for worker in sorted(workers, key=lambda item: natural_key(item.machine)):
            lines.append(
                f"{worker.machine:<13} {worker.ip_address:<16} {worker.tier:<13} "
                f"{worker.used_cpus:>3}/{worker.total_cpus:<3}  "
                f"{memory(worker.used_memory_mb):>6}/{memory(worker.total_memory_mb):<6} "
                f"{worker.used_gpus:>2}/{worker.total_gpus:<2}"
            )
        busy = sum(worker.busy for worker in workers)
        free_cpus = sum(worker.free_cpus for worker in workers)
        total_cpus = sum(worker.total_cpus for worker in workers)
        free_gpus = sum(worker.free_gpus for worker in workers)
        total_gpus = sum(worker.total_gpus for worker in workers)
        return "\n".join(
            lines
            + [
                f"\n{len(workers)} connected workers; {busy} busy, "
                f"{len(workers) - busy} available; "
                f"CPUs {free_cpus}/{total_cpus} available; "
                f"GPUs {free_gpus}/{total_gpus} available"
            ]
        )
    raise ValueError(f"unknown snapshot view: {view}")


class AallClusterApp:
    def __init__(
        self,
        client: CondorClient,
        refresh_seconds: float = 2.0,
        initial_view: str = "jobs",
    ):
        views = {name for name, _key, _label in TABS}
        self.client = client
        self.refresh_seconds = max(0.0, refresh_seconds)
        self.state = AppState(view=initial_view if initial_view in views else "jobs")
        self.colors_enabled = False

    def run(self) -> None:
        curses.wrapper(self._main)

    def _main(self, screen: curses.window) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.nodelay(True)
        screen.keypad(True)
        self._init_colors()
        self._apply_theme_background(screen)
        self._refresh()
        while True:
            self._draw(screen)
            key = screen.getch()
            if key != -1 and not self._handle_key(screen, key):
                return
            refresh_seconds = self._active_refresh_seconds()
            if refresh_seconds and time.monotonic() - self.state.last_refresh >= refresh_seconds:
                self._refresh()
            if key == -1:
                time.sleep(0.05)

    def _init_colors(self) -> None:
        self.colors_enabled = False
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            palette = THEME_256 if curses.COLORS >= 256 else THEME_16
            for pair_id, (foreground, background) in THEME_PAIR_ROLES.items():
                curses.init_pair(
                    pair_id,
                    palette[foreground],
                    palette[background],
                )
            self.colors_enabled = True
        except (AttributeError, curses.error):
            self.colors_enabled = False

    def _apply_theme_background(self, window: curses.window) -> None:
        try:
            window.bkgd(" ", self._pair(PAIR_TEXT))
        except (AttributeError, curses.error):
            pass

    def _active_refresh_seconds(self) -> float:
        if not self.refresh_seconds:
            return 0.0
        if self.state.view == "history":
            return max(10.0, self.refresh_seconds)
        return self.refresh_seconds

    def _pair(self, number: int) -> int:
        return curses.color_pair(number) if self.colors_enabled else 0

    def _addstr(self, window: curses.window, y: int, x: int, text: object, attr: int = 0) -> None:
        height, width = window.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        clipped = str(text)[: max(0, width - x)]
        try:
            window.addstr(y, x, clipped, attr)
        except curses.error:
            pass

    def _draw_box(
        self,
        window: curses.window,
        top: int,
        left: int,
        height: int,
        width: int,
        title: str = "",
    ) -> None:
        if height < 2 or width < 2:
            return
        attr = self._pair(PAIR_BORDER) | curses.A_DIM
        right = left + width - 1
        bottom = top + height - 1
        self._addstr(window, top, left, "╭", attr)
        self._addstr(window, top, right, "╮", attr)
        self._addstr(window, bottom, left, "╰", attr)
        self._addstr(window, bottom, right, "╯", attr)
        for x in range(left + 1, right):
            self._addstr(window, top, x, "─", attr)
            self._addstr(window, bottom, x, "─", attr)
        for y in range(top + 1, bottom):
            self._addstr(window, y, left, "│", attr)
            self._addstr(window, y, right, "│", attr)
        if title:
            self._addstr(window, top, left + 2, truncate(title, max(1, width - 4)), self._pair(PAIR_ACCENT) | curses.A_BOLD)

    def _refresh(self) -> None:
        try:
            view = self.state.view
            if view == "jobs":
                self.state.jobs = self.client.fetch_jobs()
                self.state.message = ""
            elif view == "workers":
                self.state.workers = self.client.fetch_workers()
                self.state.jobs = self.client.fetch_jobs()
                self.state.message = ""
            elif view == "history":
                seconds, _label = JOB_WINDOWS[self.state.history_window_index]
                self.state.history = self.client.fetch_job_activity(seconds)
                self.state.message = ""
        except CondorError as exc:
            self.state.message = str(exc)
        self.state.last_refresh = time.monotonic()
        self._clamp()

    def _switch(self, view: str) -> None:
        if view == self.state.view:
            return
        self.state.view = view
        self.state.selected = 0
        self.state.scroll = 0
        self._refresh()

    def _visible_jobs(self) -> list[Job]:
        return sorted(
            (job for job in self.state.jobs if job.active),
            key=lambda job: natural_key(job.job_id),
        )

    def _visible_history(self) -> list[Job]:
        return list(self.state.history)

    def _visible_workers(self) -> list[Worker]:
        workers = self.state.workers
        if self.state.gpu_workers_only:
            workers = [worker for worker in workers if worker.total_gpus > 0]
        return sorted(workers, key=lambda worker: natural_key(worker.machine))

    def _visible_count(self) -> int:
        if self.state.view == "jobs":
            return len(self._visible_jobs())
        if self.state.view == "workers":
            return len(self._visible_workers())
        if self.state.view == "history":
            return len(group_jobs(self._visible_history()))
        return 0

    def _clamp(self) -> None:
        count = self._visible_count()
        self.state.selected = max(0, min(self.state.selected, max(0, count - 1)))
        self.state.scroll = max(0, min(self.state.scroll, max(0, count - 1)))

    def _handle_key(self, screen: curses.window, key: int) -> bool:
        if key in (ord("q"), 27):
            return False
        for view, view_key, _label in TABS:
            if key == ord(view_key):
                self._switch(view)
                return True
        if key == ord("r"):
            self._refresh()
            return True
        if key in (curses.KEY_UP, ord("k")):
            self.state.selected -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            self.state.selected += 1
        elif key == curses.KEY_PPAGE:
            self.state.selected -= 10
        elif key == curses.KEY_NPAGE:
            self.state.selected += 10
        elif key == curses.KEY_HOME:
            self.state.selected = 0
        elif key == curses.KEY_END:
            self.state.selected = max(0, self._visible_count() - 1)
        elif self.state.view == "history" and key == ord("f"):
            self.state.history_window_index = (
                self.state.history_window_index + 1
            ) % len(JOB_WINDOWS)
            self.state.selected = self.state.scroll = 0
            self._refresh()
        elif self.state.view == "workers" and key == ord("g"):
            self.state.gpu_workers_only = not self.state.gpu_workers_only
            self.state.selected = self.state.scroll = 0
        elif key == ord("p") and self.state.view == "workers":
            self._show_worker_jobs(screen)
        self._clamp()
        return True

    def _draw(self, screen: curses.window) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            lines = [
                "Terminal size too small",
                f"Current: {width} x {height}",
                f"Needed:  {MIN_WIDTH} x {MIN_HEIGHT}",
            ]
            top = max(0, (height - len(lines)) // 2)
            for offset, line in enumerate(lines):
                self._addstr(screen, top + offset, max(0, (width - len(line)) // 2), line, curses.A_BOLD)
            screen.refresh()
            return
        self._draw_header(screen, width)
        if self.state.view == "jobs":
            self._draw_jobs(screen, height, width)
        elif self.state.view == "workers":
            self._draw_workers(screen, height, width)
        else:
            self._draw_history(screen, height, width)
        if self.state.message:
            self._addstr(screen, height - 1, 1, " error: " + self.state.message, self._pair(PAIR_BAD) | curses.A_BOLD)
        screen.refresh()

    def _draw_header(self, screen: curses.window, width: int) -> None:
        self._draw_box(screen, 0, 0, 3, width)
        x = 2
        for view, key, label in TABS:
            text = f" {key} {label} "
            attr = self._pair(PAIR_ACTIVE) | curses.A_BOLD if self.state.view == view else self._pair(PAIR_MUTED)
            self._addstr(screen, 1, x, text, attr)
            x += len(text) + 1
        title = " AALL Cluster "
        right = time.strftime("%H:%M:%S")
        title_x = max(x, (width - len(title)) // 2)
        right_x = width - len(right) - 2
        if title_x + len(title) < right_x:
            self._addstr(screen, 1, title_x, title, self._pair(PAIR_TITLE) | curses.A_BOLD)
        if x < right_x:
            self._addstr(screen, 1, right_x, right, self._pair(PAIR_MUTED))

        if self.state.view == "jobs":
            x = 1
        elif self.state.view == "workers":
            x = 1
            gpu_text = " g gpu-workers "
            self._addstr(
                screen,
                3,
                x,
                gpu_text,
                self._pair(PAIR_ACTIVE if self.state.gpu_workers_only else PAIR_MUTED),
            )
            x += len(gpu_text) + 1
            peek_text = " p peek "
            self._addstr(screen, 3, x, peek_text, self._pair(PAIR_MUTED))
            x += len(peek_text) + 1
        else:
            x = self._draw_header_choice(
                screen,
                1,
                " f filter: ",
                [(label, label) for _seconds, label in JOB_WINDOWS],
                JOB_WINDOWS[self.state.history_window_index][1],
            )

        quit_text = "q quit"
        self._addstr(
            screen,
            3,
            max(x, width - len(quit_text) - 2),
            quit_text,
            self._pair(PAIR_MUTED),
        )

    def _draw_header_choice(
        self,
        screen: curses.window,
        x: int,
        prefix: str,
        options: list[tuple[str, str]],
        active: str,
    ) -> int:
        self._addstr(screen, 3, x, prefix, self._pair(PAIR_MUTED))
        x += len(prefix)
        for index, (value, label) in enumerate(options):
            if index:
                self._addstr(screen, 3, x, " / ", self._pair(PAIR_MUTED))
                x += 3
            attr = (
                self._pair(PAIR_ACTIVE) | curses.A_BOLD
                if value == active
                else self._pair(PAIR_MUTED)
            )
            self._addstr(screen, 3, x, label, attr)
            x += len(label)
        return x + 2

    def _fit_columns(self, columns: Sequence[Column], rows: Sequence[object], available: int) -> tuple[list[Column], list[int]]:
        visible = list(columns)
        while visible:
            widths = []
            for column in visible:
                content = max([len(column.label), *(len(str(column.value(row))) for row in rows)], default=len(column.label))
                widths.append(max(column.minimum, min(column.maximum, content)))
            while sum(widths) + max(0, len(widths) - 1) > available:
                candidates = [index for index, (width, column) in enumerate(zip(widths, visible)) if width > column.minimum]
                if not candidates:
                    break
                widest = max(candidates, key=lambda index: widths[index] - visible[index].minimum)
                widths[widest] -= 1
            if sum(widths) + max(0, len(widths) - 1) <= available or len(visible) <= 2:
                return visible, widths
            visible.pop()
        return [], []

    def _draw_table(
        self,
        screen: curses.window,
        rows: Sequence[object],
        columns: Sequence[Column],
        title: str,
        color: Callable[[object], int],
        height: int,
        width: int,
    ) -> None:
        table_top = 5
        table_height = max(4, height - table_top)
        self._draw_box(screen, table_top, 0, table_height, width, f" {title} ")
        fitted, widths = self._fit_columns(columns, rows, max(1, width - 4))
        x = 2
        for column, column_width in zip(fitted, widths):
            self._addstr(screen, table_top + 1, x, column.label[:column_width].ljust(column_width), self._pair(PAIR_MUTED) | curses.A_BOLD)
            x += column_width + 1
        body_rows = max(0, table_height - 3)
        if self.state.selected < self.state.scroll:
            self.state.scroll = self.state.selected
        if self.state.selected >= self.state.scroll + max(1, body_rows):
            self.state.scroll = self.state.selected - body_rows + 1
        for offset, row in enumerate(rows[self.state.scroll : self.state.scroll + body_rows]):
            index = self.state.scroll + offset
            attr = self._pair(color(row))
            if index == self.state.selected:
                attr |= curses.A_REVERSE
            x = 2
            for column, column_width in zip(fitted, widths):
                value = truncate(column.value(row), column_width).ljust(column_width)
                self._addstr(screen, table_top + 2 + offset, x, value, attr)
                x += column_width + 1
        if rows and body_rows:
            page_count = (len(rows) + body_rows - 1) // body_rows
            current_page = min(page_count, self.state.selected // body_rows + 1)
            page = f" {current_page}/{page_count} "
            self._addstr(screen, table_top + table_height - 1, width - len(page) - 2, page, self._pair(PAIR_ACCENT) | curses.A_BOLD)

    def _draw_jobs(self, screen: curses.window, height: int, width: int) -> None:
        jobs = self._visible_jobs()
        columns = (
            Column("JOBID", 10, 16, lambda row: row.job_id),
            Column("OWNER", 8, 16, lambda row: row.owner_short),
            Column("JOB", 12, 28, lambda row: row.name),
            Column("STATE", 7, 11, lambda row: row.state),
            Column("CPU", 3, 5, lambda row: str(row.request_cpus)),
            Column("MEM", 5, 8, lambda row: memory(row.request_memory_mb)),
            Column("GPU", 3, 5, lambda row: str(row.request_gpus)),
            Column("HOST / REASON", 12, 30, lambda row: row.host if row.host != "-" else row.reason),
            Column("RUN_FOR", 7, 12, job_elapsed),
        )
        self._draw_table(
            screen,
            jobs,
            columns,
            f"Jobs: {_state_counts(jobs)}  ({len(jobs)} active)",
            lambda row: _job_color(row.state),
            height,
            width,
        )

    def _draw_history(self, screen: curses.window, height: int, width: int) -> None:
        jobs = self._visible_history()
        groups = group_jobs(jobs)
        columns = (
            Column("JOBID", 10, 16, lambda row: row.job_id),
            Column("JOB", 12, 28, lambda row: row.name),
            Column("REQ", 3, 5, lambda row: str(row.total)),
            Column("DONE", 4, 5, lambda row: str(row.completed)),
            Column("RUN", 3, 5, lambda row: str(row.running)),
            Column("PEND", 4, 5, lambda row: str(row.pending)),
            Column("HELD", 4, 5, lambda row: str(row.held)),
            Column("CPUS", 4, 6, lambda row: str(row.cpus)),
            Column("GPUS", 4, 6, lambda row: str(row.gpus)),
            Column("RUN_FOR", 7, 12, _group_run_for),
        )
        label = JOB_WINDOWS[self.state.history_window_index][1]
        self._draw_table(
            screen,
            groups,
            columns,
            f"History: {_state_counts(jobs)}  ({len(groups)} groups / {len(jobs)} tasks, last {label} + active)",
            lambda row: _job_color(row.dominant_state),
            height,
            width,
        )

    def _draw_workers(self, screen: curses.window, height: int, width: int) -> None:
        workers = self._visible_workers()
        cpu_width = max(
            (len(f"{worker.used_cpus}/{worker.total_cpus}") for worker in workers),
            default=4,
        )
        memory_values = [
            f"{memory(worker.used_memory_mb)}/{memory(worker.total_memory_mb)}"
            for worker in workers
        ]
        memory_width = max((len(value) for value in memory_values), default=4)
        gpu_width = max(
            (len(f"{worker.used_gpus}/{worker.total_gpus}") for worker in workers),
            default=3,
        )

        def cpu_meter(row: Worker) -> str:
            return _resource_meter(
                row.used_cpus,
                row.total_cpus,
                meter_width=16,
                count_width=cpu_width,
            )

        def memory_meter(row: Worker) -> str:
            return _resource_text_meter(
                f"{memory(row.used_memory_mb)}/{memory(row.total_memory_mb)}",
                row.used_memory_mb,
                row.total_memory_mb,
                meter_width=14,
                count_width=memory_width,
            )

        def gpu_meter(row: Worker) -> str:
            return _resource_meter(
                row.used_gpus,
                row.total_gpus,
                meter_width=12,
                count_width=gpu_width,
            )

        show_resource_bars = width - 4 >= 112
        if show_resource_bars:
            columns = (
                Column("WORKER", 10, 22, lambda row: row.machine),
                Column("IP", 7, 16, lambda row: row.ip_address),
                Column("TIER", 10, 22, lambda row: row.tier),
                Column("CPU", 24, 38, cpu_meter),
                Column("MEM", 24, 38, memory_meter),
                Column("GPU", 18, 30, gpu_meter),
            )
        else:
            columns = (
                Column("WORKER", 10, 22, lambda row: row.machine),
                Column("IP", 7, 16, lambda row: row.ip_address),
                Column("TIER", 10, 22, lambda row: row.tier),
                Column("CPU", max(4, cpu_width), max(8, cpu_width), lambda row: f"{row.used_cpus}/{row.total_cpus}"),
                Column("MEM", max(4, memory_width), max(12, memory_width), lambda row: f"{memory(row.used_memory_mb)}/{memory(row.total_memory_mb)}"),
                Column("GPU", max(3, gpu_width), max(8, gpu_width), lambda row: f"{row.used_gpus}/{row.total_gpus}"),
            )
        busy = sum(worker.busy for worker in self.state.workers)
        free_cpus = sum(worker.free_cpus for worker in self.state.workers)
        total_cpus = sum(worker.total_cpus for worker in self.state.workers)
        free_gpus = sum(worker.free_gpus for worker in self.state.workers)
        total_gpus = sum(worker.total_gpus for worker in self.state.workers)
        title = (
            f"Workers: {len(self.state.workers)} connected  {busy} busy  "
            f"{len(self.state.workers) - busy} available  "
            f"CPUs {free_cpus}/{total_cpus} available  "
            f"GPUs {free_gpus}/{total_gpus} available"
        )
        self._draw_table(
            screen,
            workers,
            columns,
            title,
            _worker_color,
            height,
            width,
        )

    def _selected_worker(self) -> Worker | None:
        workers = self._visible_workers()
        return workers[self.state.selected] if workers else None

    def _show_worker_jobs(self, screen: curses.window) -> None:
        worker = self._selected_worker()
        if worker is None:
            return
        jobs = [job for job in self.state.jobs if job.host == worker.machine]
        lines = [
            f"{job.job_id:<10} {job.owner_short:<15} {job.state:<10} cpu={job.request_cpus:<3} mem={memory(job.request_memory_mb):<7} {job.name}"
            for job in jobs
        ]
        self._popup(screen, f" jobs on {worker.machine} ", "\n".join(lines) if lines else "No jobs currently assigned to this worker.")

    def _popup(self, screen: curses.window, title: str, text: str) -> None:
        screen.nodelay(False)
        height, width = screen.getmaxyx()
        top, left, popup_height, popup_width = popup_geometry(
            height, width, title, text
        )
        window = curses.newwin(popup_height, popup_width, top, left)
        window.keypad(True)
        self._apply_theme_background(window)
        lines = _popup_lines(text, popup_width)
        scroll = 0
        page = max(1, popup_height - 4)
        while True:
            window.erase()
            self._draw_box(window, 0, 0, popup_height, popup_width, title)
            for offset, line in enumerate(lines[scroll : scroll + page]):
                self._addstr(window, 1 + offset, 2, line, self._pair(PAIR_TEXT))
            hint = f" {scroll + 1 if lines else 0}-{min(len(lines), scroll + page)}/{len(lines)}  arrows scroll  q/esc close "
            self._addstr(window, popup_height - 1, max(2, popup_width - len(hint) - 2), hint, self._pair(PAIR_ACCENT))
            window.refresh()
            key = window.getch()
            if key in (ord("q"), 27, ord("d"), ord("p"), ord("a")):
                break
            if key in (curses.KEY_DOWN, ord("j")):
                scroll = min(max(0, len(lines) - page), scroll + 1)
            elif key in (curses.KEY_UP, ord("k")):
                scroll = max(0, scroll - 1)
            elif key == curses.KEY_NPAGE:
                scroll = min(max(0, len(lines) - page), scroll + page)
            elif key == curses.KEY_PPAGE:
                scroll = max(0, scroll - page)
            elif key == curses.KEY_HOME:
                scroll = 0
            elif key == curses.KEY_END:
                scroll = max(0, len(lines) - page)
        screen.nodelay(True)
        screen.touchwin()
