"""Payload construction and enum validation for todoTask objects."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

from .dates import due_to_graph, parse_when, to_graph
from .errors import UsageError

STATUSES = ("notStarted", "inProgress", "completed", "waitingOnOthers", "deferred")
IMPORTANCES = ("low", "normal", "high")
WELLKNOWN_LISTS = ("none", "defaultList", "flaggedEmails")

_STATUS_ALIASES = {
    "todo": "notStarted", "not-started": "notStarted", "notstarted": "notStarted", "open": "notStarted",
    "doing": "inProgress", "in-progress": "inProgress", "inprogress": "inProgress", "wip": "inProgress",
    "done": "completed", "complete": "completed", "completed": "completed",
    "waiting": "waitingOnOthers", "blocked": "waitingOnOthers", "waitingonothers": "waitingOnOthers",
    "deferred": "deferred", "later": "deferred", "someday": "deferred",
}

_DAY_NAMES = {
    "mon": "monday", "tue": "tuesday", "tues": "tuesday", "wed": "wednesday",
    "thu": "thursday", "thur": "thursday", "thurs": "thursday", "fri": "friday",
    "sat": "saturday", "sun": "sunday",
}
_FULL_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def normalize_status(value: str | None) -> str | None:
    if value is None:
        return None
    key = value.strip()
    if key in STATUSES:
        return key
    resolved = _STATUS_ALIASES.get(key.lower().replace("_", "-"))
    if resolved:
        return resolved
    raise UsageError(
        f"unknown status {value!r}",
        hint="one of: " + ", ".join(STATUSES) + " (aliases: todo, doing, done, blocked, later)",
    )


def normalize_importance(value: str | None) -> str | None:
    if value is None:
        return None
    key = value.strip().lower()
    if key in ("!", "!!", "high", "urgent"):
        return "high"
    if key in ("low", "meh"):
        return "low"
    if key in ("normal", "medium", "default"):
        return "normal"
    raise UsageError(f"unknown importance {value!r}", hint="one of: " + ", ".join(IMPORTANCES))


def normalize_day(value: str) -> str:
    key = value.strip().lower()
    key = _DAY_NAMES.get(key, key)
    if key not in _FULL_DAYS:
        raise UsageError(f"unknown day of week {value!r}", hint="e.g. mon, tuesday, sat")
    return key


def build_recurrence(
    spec: str,
    *,
    anchor: dt.datetime,
    until: dt.datetime | None = None,
    count: int | None = None,
) -> dict[str, Any]:
    """Turn a compact recurrence spec into a Graph ``patternedRecurrence``.

    Accepted forms::

        daily              daily:2            (every other day)
        weekly             weekly:mon,thu     (defaults to the anchor's weekday)
        monthly            monthly:15         (defaults to the anchor's day)
        yearly

    ``anchor`` seeds the recurrence range start and any unspecified day/month,
    and is normally the task's due date.
    """
    if until is not None and count is not None:
        raise UsageError("give either an end date or an occurrence count, not both")

    head, _, arg = spec.strip().lower().partition(":")
    pattern: dict[str, Any]

    if head in ("daily", "day"):
        pattern = {"type": "daily", "interval": _interval(arg)}
    elif head in ("weekday", "weekdays"):
        pattern = {
            "type": "weekly",
            "interval": 1,
            "daysOfWeek": ["monday", "tuesday", "wednesday", "thursday", "friday"],
        }
    elif head in ("weekly", "week"):
        days = [normalize_day(d) for d in arg.split(",") if d.strip()] or [_FULL_DAYS[anchor.weekday()]]
        pattern = {"type": "weekly", "interval": 1, "daysOfWeek": days}
    elif head in ("biweekly", "fortnightly"):
        days = [normalize_day(d) for d in arg.split(",") if d.strip()] or [_FULL_DAYS[anchor.weekday()]]
        pattern = {"type": "weekly", "interval": 2, "daysOfWeek": days}
    elif head in ("monthly", "month"):
        day_of_month = int(arg) if arg.strip().isdigit() else anchor.day
        if not 1 <= day_of_month <= 31:
            raise UsageError(f"day of month must be 1-31, got {day_of_month}")
        pattern = {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": day_of_month}
    elif head in ("yearly", "year", "annually"):
        pattern = {
            "type": "absoluteYearly",
            "interval": 1,
            "dayOfMonth": anchor.day,
            "month": anchor.month,
        }
    else:
        raise UsageError(
            f"unknown recurrence {spec!r}",
            hint="try daily, daily:2, weekdays, weekly:mon,thu, biweekly, monthly:15 or yearly",
        )

    recurrence_range: dict[str, Any] = {"type": "noEnd", "startDate": anchor.date().isoformat()}
    if until is not None:
        recurrence_range = {
            "type": "endDate",
            "startDate": anchor.date().isoformat(),
            "endDate": until.date().isoformat(),
        }
    elif count is not None:
        if count < 1:
            raise UsageError("occurrence count must be at least 1")
        recurrence_range = {
            "type": "numbered",
            "startDate": anchor.date().isoformat(),
            "numberOfOccurrences": count,
        }

    return {"pattern": pattern, "range": recurrence_range}


def _interval(arg: str) -> int:
    if not arg.strip():
        return 1
    if not arg.strip().isdigit() or int(arg) < 1:
        raise UsageError(f"recurrence interval must be a positive integer, got {arg!r}")
    return int(arg)


class TaskPayloadBuilder:
    """Accumulates a todoTask body from CLI-shaped inputs.

    Distinguishes "not mentioned" from "explicitly cleared": ``clear('dueDateTime')``
    emits ``null``, which is how Graph removes a value, whereas an untouched field
    is simply absent from the PATCH.
    """

    def __init__(self, *, tz_name: str, due_tz: str | None = None, now: dt.datetime | None = None) -> None:
        self.tz_name = tz_name
        self.due_tz = due_tz
        self.now = now
        self.body: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> TaskPayloadBuilder:
        if value is not None:
            self.body[key] = value
        return self

    def clear(self, key: str) -> TaskPayloadBuilder:
        self.body[key] = None
        return self

    def title(self, value: str | None) -> TaskPayloadBuilder:
        if value is not None:
            if not value.strip():
                raise UsageError("task title cannot be empty")
            self.body["title"] = value.strip()
        return self

    def note(self, text: str | None, *, content_type: str = "text") -> TaskPayloadBuilder:
        if text is not None:
            self.body["body"] = {"content": text, "contentType": content_type}
        return self

    def status(self, value: str | None) -> TaskPayloadBuilder:
        return self.set("status", normalize_status(value))

    def importance(self, value: str | None) -> TaskPayloadBuilder:
        return self.set("importance", normalize_importance(value))

    def categories(self, values: Iterable[str] | None) -> TaskPayloadBuilder:
        if values is not None:
            flat = [c.strip() for v in values for c in str(v).split(",") if c.strip()]
            self.body["categories"] = flat
        return self

    def due(self, expression: str | None) -> TaskPayloadBuilder:
        if expression is None:
            return self
        moment, _ = parse_when(expression, now=self.now, tz_name=self.tz_name)
        self.body["dueDateTime"] = due_to_graph(moment, self.due_tz)
        self._due_anchor = moment
        return self

    def start(self, expression: str | None) -> TaskPayloadBuilder:
        if expression is None:
            return self
        moment, _ = parse_when(expression, now=self.now, tz_name=self.tz_name)
        self.body["startDateTime"] = to_graph(moment, self.tz_name)
        return self

    def reminder(self, expression: str | None) -> TaskPayloadBuilder:
        if expression is None:
            return self
        moment, has_time = parse_when(expression, now=self.now, tz_name=self.tz_name)
        if not has_time:
            # A reminder with no clock time would fire at midnight, which nobody wants.
            moment = moment.replace(hour=9, minute=0)
        self.body["reminderDateTime"] = to_graph(moment, self.tz_name)
        self.body["isReminderOn"] = True
        return self

    def recurrence(
        self,
        spec: str | None,
        *,
        until: str | None = None,
        count: int | None = None,
    ) -> TaskPayloadBuilder:
        if spec is None:
            return self
        anchor = getattr(self, "_due_anchor", None) or (self.now or dt.datetime.now())
        until_moment = parse_when(until, now=self.now, tz_name=self.tz_name)[0] if until else None
        self.body["recurrence"] = build_recurrence(spec, anchor=anchor, until=until_moment, count=count)
        return self

    def build(self) -> dict[str, Any]:
        return dict(self.body)
