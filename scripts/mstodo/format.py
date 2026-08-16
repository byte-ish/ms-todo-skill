"""Terminal rendering. JSON output never passes through here."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
from collections.abc import Iterable, Sequence
from typing import Any

from .dates import from_graph, humanize

STATUS_GLYPH = {
    "notStarted": "[ ]",
    "inProgress": "[~]",
    "completed": "[x]",
    "waitingOnOthers": "[w]",
    "deferred": "[>]",
}

_ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "blue": "\033[34m",
}

ID_WIDTH = 12


def use_color(stream: Any = None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("MSTODO_FORCE_COLOR"):
        return True
    if os.environ.get("TERM", "") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def paint(text: str, style: str, *, enabled: bool) -> str:
    if not enabled or not text:
        return text
    return f"{_ANSI.get(style, '')}{text}{_ANSI['reset']}"


def terminal_width(default: int = 100) -> int:
    try:
        return max(60, shutil.get_terminal_size((default, 24)).columns)
    except OSError:  # pragma: no cover
        return default


def short_id(value: str | None, width: int = ID_WIDTH) -> str:
    if not value:
        return ""
    return value if len(value) <= width else value[:width]


def dump_json(payload: Any, *, stream: Any = None) -> None:
    stream = stream or sys.stdout
    json.dump(payload, stream, indent=2, ensure_ascii=False, default=str)
    stream.write("\n")


def dump_jsonl(rows: Iterable[Any], *, stream: Any = None) -> None:
    stream = stream or sys.stdout
    for row in rows:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _truncate(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    if width <= 1 or len(text) <= width:
        return text
    return text[: width - 1] + "…"


def render_table(rows: Sequence[Sequence[str]], headers: Sequence[str], *, color: bool) -> str:
    if not rows:
        return ""
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def line(cells: Sequence[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    out = [paint(line(headers), "bold", enabled=color)]
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def render_lists(lists: Sequence[dict[str, Any]], *, color: bool = False) -> str:
    if not lists:
        return "no task lists"
    rows = []
    for item in lists:
        flags = []
        if item.get("wellknownListName") not in (None, "none"):
            flags.append(str(item["wellknownListName"]))
        if item.get("isShared"):
            flags.append("shared")
        if item.get("isOwner") is False:
            flags.append("not-owner")
        rows.append(
            [
                short_id(item.get("id")),
                str(item.get("displayName", "")),
                ",".join(flags),
            ]
        )
    return render_table(rows, ["ID", "NAME", "FLAGS"], color=color)


def render_tasks(
    tasks: Sequence[dict[str, Any]],
    *,
    color: bool = False,
    now: dt.datetime | None = None,
    show_list: bool = False,
    width: int | None = None,
) -> str:
    if not tasks:
        return "no tasks"

    now = now or dt.datetime.now(dt.timezone.utc)
    width = width or terminal_width()
    # Everything except the title has a bounded width; the title absorbs the rest.
    fixed = 3 + 2 + ID_WIDTH + 2 + 12 + 2 + 1 + 2 + (20 if show_list else 0)
    title_width = max(20, width - fixed - 12)

    rows = []
    for task in tasks:
        status = str(task.get("status", ""))
        glyph = STATUS_GLYPH.get(status, "[?]")
        due = from_graph(task.get("dueDateTime"))
        due_text = humanize(due, now=now)
        overdue = due is not None and due < now and status != "completed"

        importance = str(task.get("importance", "normal"))
        mark = {"high": "!", "low": "v"}.get(importance, " ")

        title = _truncate(task.get("title", ""), title_width)
        if status == "completed":
            title = paint(title, "dim", enabled=color)
        elif overdue:
            title = paint(title, "red", enabled=color)
        elif importance == "high":
            title = paint(title, "yellow", enabled=color)

        extras = []
        if task.get("recurrence"):
            extras.append("repeats")
        if task.get("isReminderOn"):
            extras.append("reminder")
        if task.get("hasAttachments"):
            extras.append("attach")
        items = task.get("checklistItems")
        if items:
            done = sum(1 for i in items if i.get("isChecked"))
            extras.append(f"{done}/{len(items)}")
        for category in task.get("categories") or []:
            extras.append(f"#{category}")

        row = [glyph, short_id(task.get("id")), paint(due_text, "red", enabled=color and overdue), mark, title]
        if show_list:
            row.insert(2, _truncate(str(task.get("_listName", "")), 18))
        row.append(paint(" ".join(extras), "dim", enabled=color))
        rows.append(row)

    headers = ["ST", "ID", "DUE", "!", "TITLE", "NOTES"]
    if show_list:
        headers.insert(2, "LIST")
    return render_table(rows, headers, color=color)


def render_task_detail(task: dict[str, Any], *, color: bool = False) -> str:
    lines = [paint(str(task.get("title", "")), "bold", enabled=color)]
    fields = [
        ("id", task.get("id")),
        ("status", task.get("status")),
        ("importance", task.get("importance")),
        ("due", _fmt_dtz(task.get("dueDateTime"))),
        ("start", _fmt_dtz(task.get("startDateTime"))),
        ("reminder", _fmt_dtz(task.get("reminderDateTime")) if task.get("isReminderOn") else None),
        ("completed", _fmt_dtz(task.get("completedDateTime"))),
        ("categories", ", ".join(task.get("categories") or []) or None),
        ("recurrence", _fmt_recurrence(task.get("recurrence"))),
        ("created", task.get("createdDateTime")),
        ("modified", task.get("lastModifiedDateTime")),
    ]
    for label, value in fields:
        if value:
            lines.append(f"  {paint(label + ':', 'dim', enabled=color)} {value}")

    body = (task.get("body") or {}).get("content", "").strip()
    if body:
        lines.append("")
        lines.extend("  " + line for line in body.splitlines())

    checklist = task.get("checklistItems") or []
    if checklist:
        lines.append("")
        lines.append(paint("  checklist:", "dim", enabled=color))
        for item in checklist:
            box = "[x]" if item.get("isChecked") else "[ ]"
            lines.append(f"    {box} {short_id(item.get('id'), 8)}  {item.get('displayName', '')}")

    links = task.get("linkedResources") or []
    if links:
        lines.append("")
        lines.append(paint("  linked:", "dim", enabled=color))
        for link in links:
            target = link.get("webUrl") or link.get("externalId") or ""
            lines.append(f"    {link.get('displayName', '')}  {target}".rstrip())

    return "\n".join(lines)


def _fmt_dtz(value: dict | None) -> str | None:
    moment = from_graph(value)
    if moment is None:
        return None
    if (moment.hour, moment.minute) == (0, 0):
        return moment.strftime("%Y-%m-%d")
    return moment.strftime("%Y-%m-%d %H:%M %Z").strip()


def _fmt_recurrence(recurrence: dict | None) -> str | None:
    if not recurrence:
        return None
    pattern = recurrence.get("pattern") or {}
    rng = recurrence.get("range") or {}
    parts = [str(pattern.get("type", "?"))]
    if pattern.get("interval", 1) != 1:
        parts.append(f"every {pattern['interval']}")
    if pattern.get("daysOfWeek"):
        parts.append(",".join(d[:3] for d in pattern["daysOfWeek"]))
    if pattern.get("dayOfMonth"):
        parts.append(f"day {pattern['dayOfMonth']}")
    if rng.get("type") == "endDate":
        parts.append(f"until {rng.get('endDate')}")
    elif rng.get("type") == "numbered":
        parts.append(f"x{rng.get('numberOfOccurrences')}")
    return " ".join(parts)
