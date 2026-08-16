"""Human date input to Graph ``dateTimeTimeZone``, and back.

Two conventions matter here and both are deliberate:

* **Due dates are sent as midnight UTC.** To Do renders ``dueDateTime`` as a
  bare calendar date. Sending midnight in a non-UTC zone makes every client east
  or west of UTC display the neighbouring day, which is the single most common
  bug in To Do integrations. ``--due-tz`` overrides when you genuinely want a
  zoned instant.
* **Reminders and start times are sent in the user's zone**, because those are
  real instants the user expects an alert at.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - 3.8 and older are unsupported
    ZoneInfo = None  # type: ignore[assignment]

    class ZoneInfoNotFoundError(Exception):  # type: ignore[no-redef]
        pass


from .errors import UsageError

#: Graph's Outlook-flavoured datetime literal: no offset, seven fractional digits.
GRAPH_DATETIME_FMT = "%Y-%m-%dT%H:%M:%S.0000000"

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}

_ISO_DATE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")
_REL = re.compile(r"^([+-]?)(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)$")
_IN_REL = re.compile(r"^in\s+(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)$")
_TIME_12 = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$")
_TIME_24 = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def _canonical_unit(unit: str) -> str:
    return {"min": "m", "mins": "m", "minute": "m", "minutes": "m",
            "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
            "day": "d", "days": "d",
            "week": "w", "weeks": "w"}.get(unit, unit[0])


def local_timezone_name() -> str:
    """Best-effort IANA zone name for this machine, defaulting to UTC."""
    env = os.environ.get("TZ")
    if env and _zone_or_none(env) is not None:
        return env
    link = Path("/etc/localtime")
    if link.is_symlink():
        target = os.readlink(link)
        if "zoneinfo/" in target:
            candidate = target.split("zoneinfo/", 1)[1]
            if _zone_or_none(candidate) is not None:
                return candidate
    return "UTC"


def _zone_or_none(name: str) -> dt.tzinfo | None:
    if name.upper() == "UTC":
        return dt.timezone.utc
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def zone(name: str) -> dt.tzinfo:
    """Resolve an IANA zone name, with a clear error rather than a silent UTC."""
    resolved = _zone_or_none(name)
    if resolved is None:
        raise UsageError(
            f"unknown time zone {name!r}",
            hint="use an IANA name such as Europe/London or Asia/Kolkata. "
            "On a slim Python install you may need: pip install tzdata",
        )
    return resolved


def _parse_time_token(token: str) -> dt.time | None:
    match = _TIME_12.match(token)
    if match:
        hour = int(match.group(1)) % 12
        minute = int(match.group(2) or 0)
        if match.group(3) == "pm":
            hour += 12
        if hour > 23 or minute > 59:
            return None
        return dt.time(hour, minute)
    match = _TIME_24.match(token)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        second = int(match.group(3) or 0)
        if hour > 23 or minute > 59 or second > 59:
            return None
        return dt.time(hour, minute, second)
    return None


def _parse_date_token(token: str, today: dt.date) -> tuple[dt.date, dt.time | None] | None:
    """Return (date, implied_time) for a bare date word, or None if unrecognised."""
    match = _ISO_DATE.match(token)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return dt.date(year, month, day), None
        except ValueError as exc:
            raise UsageError(f"invalid date {token!r}: {exc}") from exc

    if token in ("today", "tod"):
        return today, None
    if token in ("tomorrow", "tmr", "tom"):
        return today + dt.timedelta(days=1), None
    if token == "yesterday":
        return today - dt.timedelta(days=1), None
    if token == "tonight":
        return today, dt.time(20, 0)
    if token in ("eod", "endofday"):
        return today, dt.time(17, 0)
    if token in ("eow", "endofweek"):
        return today + dt.timedelta(days=(4 - today.weekday()) % 7), dt.time(17, 0)

    if token in _WEEKDAYS:
        # A bare weekday always means the *next* such day, never today, which is
        # what people mean by "move it to friday" on a Friday afternoon.
        delta = (_WEEKDAYS[token] - today.weekday()) % 7 or 7
        return today + dt.timedelta(days=delta), None

    match = _REL.match(token)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        amount = int(match.group(2))
        unit = _canonical_unit(match.group(3))
        if unit in ("d", "w"):
            days = amount * (7 if unit == "w" else 1)
            return today + dt.timedelta(days=sign * days), None
    return None


def parse_when(
    text: str,
    *,
    now: dt.datetime | None = None,
    tz_name: str | None = None,
) -> tuple[dt.datetime, bool]:
    """Parse a human date/time expression.

    Returns the resolved local datetime and whether an explicit time was given.
    Accepts: ISO dates, ``today``/``tomorrow``/``tonight``/``yesterday``,
    weekday names (optionally prefixed with ``next``), ``eod``/``eow``,
    relative offsets (``+3d``, ``2w``, ``in 4 hours``), clock times
    (``17:00``, ``5pm``), and any date token followed by a time token.
    """
    tz_name = tz_name or local_timezone_name()
    tzinfo = zone(tz_name)
    now = now.astimezone(tzinfo) if now else dt.datetime.now(tzinfo)

    raw = " ".join(text.strip().lower().split())
    if not raw:
        raise UsageError("empty date expression")

    # ISO datetime, with either a T or a space separating date from time. Input
    # is already lower-cased, so the separator and any trailing Z need folding.
    if re.match(r"^\d{4}-\d{2}-\d{2}[t ]\d", raw):
        iso_candidate = raw.replace(" ", "T", 1).upper().replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(iso_candidate)
        except ValueError as exc:
            raise UsageError(f"could not parse {text!r} as an ISO datetime: {exc}") from exc
        parsed = parsed.replace(tzinfo=tzinfo) if parsed.tzinfo is None else parsed
        return parsed.astimezone(tzinfo), True

    match = _IN_REL.match(raw)
    if match:
        seconds = int(match.group(1)) * _UNIT_SECONDS[_canonical_unit(match.group(2))]
        return now + dt.timedelta(seconds=seconds), _canonical_unit(match.group(2)) in ("m", "h")

    match = _REL.match(raw)
    if match and _canonical_unit(match.group(3)) in ("m", "h"):
        sign = -1 if match.group(1) == "-" else 1
        seconds = sign * int(match.group(2)) * _UNIT_SECONDS[_canonical_unit(match.group(3))]
        return now + dt.timedelta(seconds=seconds), True

    tokens = raw.split()
    if tokens and tokens[0] == "next":
        tokens = tokens[1:]
        if tokens and tokens[0] == "week":
            tokens = ["monday", *tokens[1:]]
        elif tokens and tokens[0] == "month":
            first = (now.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
            return first.replace(hour=0, minute=0, second=0, microsecond=0), False
    if not tokens:
        raise UsageError(f"could not parse {text!r} as a date")

    # A lone time means "today at that time", rolling to tomorrow if it's past.
    only_time = _parse_time_token(tokens[0]) if len(tokens) == 1 else None
    if only_time is not None:
        candidate = dt.datetime.combine(now.date(), only_time, tzinfo=tzinfo)
        if candidate <= now:
            candidate += dt.timedelta(days=1)
        return candidate, True

    parsed_date = _parse_date_token(tokens[0], now.date())
    if parsed_date is None:
        raise UsageError(
            f"could not parse {text!r} as a date",
            hint="try 2026-08-20, tomorrow, friday, +3d, 'tomorrow 5pm' or '2026-08-20 17:00'",
        )

    date_part, implied_time = parsed_date
    explicit_time = None
    if len(tokens) > 1:
        rest = tokens[1]
        if rest == "at" and len(tokens) > 2:
            rest = tokens[2]
        explicit_time = _parse_time_token(rest)
        if explicit_time is None:
            raise UsageError(
                f"could not parse the time part of {text!r}",
                hint="times look like 17:00, 5pm or 5:30pm",
            )

    time_part = explicit_time or implied_time
    resolved = dt.datetime.combine(date_part, time_part or dt.time(0, 0), tzinfo=tzinfo)
    return resolved, time_part is not None


def to_graph(moment: dt.datetime, tz_name: str) -> dict[str, str]:
    """Render a datetime as Graph's ``dateTimeTimeZone``."""
    target = zone(tz_name)
    local = moment.astimezone(target) if moment.tzinfo else moment.replace(tzinfo=target)
    return {"dateTime": local.strftime(GRAPH_DATETIME_FMT), "timeZone": tz_name}


def due_to_graph(moment: dt.datetime, tz_name: str | None = None) -> dict[str, str]:
    """Render a due date. Defaults to midnight UTC — see the module docstring."""
    if tz_name:
        return to_graph(moment, tz_name)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return {"dateTime": midnight.strftime(GRAPH_DATETIME_FMT), "timeZone": "UTC"}


def from_graph(value: dict | None) -> dt.datetime | None:
    """Parse a Graph ``dateTimeTimeZone`` back into an aware datetime."""
    if not value or not value.get("dateTime"):
        return None
    raw = str(value["dateTime"])
    # Python's fromisoformat rejects 7-digit fractions before 3.11.
    if "." in raw:
        head, frac = raw.split(".", 1)
        raw = f"{head}.{frac[:6]}"
    try:
        naive = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    tzinfo = _zone_or_none(str(value.get("timeZone") or "UTC")) or dt.timezone.utc
    return naive.replace(tzinfo=tzinfo) if naive.tzinfo is None else naive.astimezone(tzinfo)


def parse_timestamp(raw: str | None) -> dt.datetime | None:
    """Parse a plain ISO 8601 timestamp such as ``lastModifiedDateTime``."""
    if not raw:
        return None
    text = raw.replace("Z", "+00:00")
    if "." in text:
        head, rest = text.split(".", 1)
        digits = "".join(ch for ch in rest if ch.isdigit())[:6]
        suffix = rest[len(digits):] if rest[len(digits):].startswith(("+", "-")) else ""
        text = f"{head}.{digits}{suffix or '+00:00'}"
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def humanize(moment: dt.datetime | None, *, now: dt.datetime | None = None) -> str:
    """Short, scannable rendering used by the table output."""
    if moment is None:
        return ""
    now = now or dt.datetime.now(moment.tzinfo or dt.timezone.utc)
    delta_days = (moment.date() - now.date()).days
    time_part = "" if (moment.hour, moment.minute) == (0, 0) else moment.strftime(" %H:%M")
    if delta_days == 0:
        return f"today{time_part}"
    if delta_days == 1:
        return f"tomorrow{time_part}"
    if delta_days == -1:
        return f"yesterday{time_part}"
    if -7 < delta_days < 7:
        return moment.strftime("%a") + time_part
    return moment.strftime("%Y-%m-%d") + time_part
