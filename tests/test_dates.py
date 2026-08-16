import datetime as dt

import pytest

from mstodo.dates import (
    due_to_graph,
    from_graph,
    humanize,
    parse_timestamp,
    parse_when,
    to_graph,
)
from mstodo.errors import UsageError

# A Wednesday, so "friday" and "monday" resolve in opposite directions.
NOW = dt.datetime(2026, 8, 19, 14, 30, tzinfo=dt.timezone.utc)


def parse(text: str):
    return parse_when(text, now=NOW, tz_name="UTC")


@pytest.mark.parametrize(
    "text,expected_date",
    [
        ("today", dt.date(2026, 8, 19)),
        ("tomorrow", dt.date(2026, 8, 20)),
        ("yesterday", dt.date(2026, 8, 18)),
        ("2026-09-01", dt.date(2026, 9, 1)),
        ("2026/09/01", dt.date(2026, 9, 1)),
        ("+3d", dt.date(2026, 8, 22)),
        ("2w", dt.date(2026, 9, 2)),
        ("friday", dt.date(2026, 8, 21)),
        ("next friday", dt.date(2026, 8, 21)),
        ("mon", dt.date(2026, 8, 24)),
        ("next week", dt.date(2026, 8, 24)),
        ("next month", dt.date(2026, 9, 1)),
    ],
)
def test_date_expressions(text, expected_date):
    moment, _ = parse(text)
    assert moment.date() == expected_date


def test_bare_weekday_never_means_today():
    # NOW is a Wednesday; "wednesday" must mean next week, not this instant.
    moment, _ = parse("wednesday")
    assert moment.date() == dt.date(2026, 8, 26)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("tomorrow 5pm", dt.datetime(2026, 8, 20, 17, 0)),
        ("tomorrow at 5pm", dt.datetime(2026, 8, 20, 17, 0)),
        ("2026-09-01 09:30", dt.datetime(2026, 9, 1, 9, 30)),
        ("2026-09-01T09:30", dt.datetime(2026, 9, 1, 9, 30)),
        ("friday 17:45", dt.datetime(2026, 8, 21, 17, 45)),
        ("tonight", dt.datetime(2026, 8, 19, 20, 0)),
        ("eod", dt.datetime(2026, 8, 19, 17, 0)),
    ],
)
def test_datetime_expressions(text, expected):
    moment, has_time = parse(text)
    assert moment.replace(tzinfo=None) == expected
    assert has_time is True


def test_bare_time_rolls_forward_when_already_past():
    moment, has_time = parse("09:00")  # NOW is 14:30
    assert moment.date() == dt.date(2026, 8, 20)
    assert has_time is True


def test_bare_time_stays_today_when_still_ahead():
    moment, _ = parse("18:00")
    assert moment.date() == dt.date(2026, 8, 19)


def test_relative_hours_keep_the_clock_time():
    moment, has_time = parse("in 4 hours")
    assert moment == NOW + dt.timedelta(hours=4)
    assert has_time is True


def test_date_only_expressions_report_no_time():
    _, has_time = parse("tomorrow")
    assert has_time is False


@pytest.mark.parametrize("text", ["", "someday maybe", "2026-13-45", "tomorrow 25:99", "blorp"])
def test_unparseable_input_raises_usage_error(text):
    with pytest.raises(UsageError):
        parse(text)


def test_due_dates_default_to_midnight_utc():
    """The whole point: a due date must not shift a day for non-UTC users."""
    moment, _ = parse_when("2026-08-20", now=NOW, tz_name="Asia/Kolkata")
    payload = due_to_graph(moment)
    assert payload == {"dateTime": "2026-08-20T00:00:00.0000000", "timeZone": "UTC"}


def test_due_tz_override_is_honoured():
    moment, _ = parse_when("2026-08-20 17:00", now=NOW, tz_name="Europe/London")
    payload = due_to_graph(moment, "Europe/London")
    assert payload["timeZone"] == "Europe/London"
    assert payload["dateTime"].startswith("2026-08-20T17:00:00")


def test_to_graph_converts_between_zones():
    moment = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc)
    payload = to_graph(moment, "Asia/Kolkata")
    assert payload == {"dateTime": "2026-08-20T17:30:00.0000000", "timeZone": "Asia/Kolkata"}


def test_from_graph_handles_seven_digit_fractions():
    parsed = from_graph({"dateTime": "2026-08-20T17:00:00.1234567", "timeZone": "UTC"})
    assert parsed is not None
    assert parsed.hour == 17
    assert parsed.tzinfo is not None


def test_from_graph_returns_none_for_empty_values():
    assert from_graph(None) is None
    assert from_graph({}) is None
    assert from_graph({"dateTime": "not-a-date", "timeZone": "UTC"}) is None


def test_parse_timestamp_handles_graph_precision():
    parsed = parse_timestamp("2020-08-18T09:03:05.8339192Z")
    assert parsed is not None
    assert parsed.year == 2020
    assert parsed.utcoffset() == dt.timedelta(0)


def test_unknown_timezone_is_a_usage_error():
    with pytest.raises(UsageError, match="unknown time zone"):
        parse_when("today", now=NOW, tz_name="Mars/Olympus_Mons")


@pytest.mark.parametrize(
    "delta,expected",
    [(0, "today"), (1, "tomorrow"), (-1, "yesterday"), (30, "2026-09-18")],
)
def test_humanize(delta, expected):
    moment = (NOW + dt.timedelta(days=delta)).replace(hour=0, minute=0)
    assert humanize(moment, now=NOW) == expected
