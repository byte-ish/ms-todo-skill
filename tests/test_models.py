import datetime as dt

import pytest

from mstodo.errors import UsageError
from mstodo.models import (
    TaskPayloadBuilder,
    build_recurrence,
    normalize_day,
    normalize_importance,
    normalize_status,
)

# A Wednesday.
NOW = dt.datetime(2026, 8, 19, 10, 0, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("done", "completed"),
        ("Done", "completed"),
        ("todo", "notStarted"),
        ("in-progress", "inProgress"),
        ("in_progress", "inProgress"),
        ("blocked", "waitingOnOthers"),
        ("someday", "deferred"),
        ("notStarted", "notStarted"),
        (None, None),
    ],
)
def test_status_aliases(value, expected):
    assert normalize_status(value) == expected


def test_unknown_status_lists_the_valid_ones():
    with pytest.raises(UsageError) as excinfo:
        normalize_status("finished-ish")
    assert "waitingOnOthers" in (excinfo.value.hint or "")


@pytest.mark.parametrize(
    "value,expected",
    [("!", "high"), ("urgent", "high"), ("HIGH", "high"), ("medium", "normal"), ("low", "low")],
)
def test_importance_aliases(value, expected):
    assert normalize_importance(value) == expected


def test_normalize_day():
    assert normalize_day("Mon") == "monday"
    assert normalize_day("thurs") == "thursday"
    with pytest.raises(UsageError):
        normalize_day("funday")


def test_daily_recurrence():
    result = build_recurrence("daily", anchor=NOW)
    assert result["pattern"] == {"type": "daily", "interval": 1}
    assert result["range"] == {"type": "noEnd", "startDate": "2026-08-19"}


def test_daily_with_interval():
    assert build_recurrence("daily:3", anchor=NOW)["pattern"]["interval"] == 3


def test_weekly_defaults_to_the_anchor_weekday():
    pattern = build_recurrence("weekly", anchor=NOW)["pattern"]
    assert pattern == {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]}


def test_weekly_with_explicit_days():
    pattern = build_recurrence("weekly:mon,thu", anchor=NOW)["pattern"]
    assert pattern["daysOfWeek"] == ["monday", "thursday"]


def test_weekdays_shorthand():
    pattern = build_recurrence("weekdays", anchor=NOW)["pattern"]
    assert pattern["daysOfWeek"] == ["monday", "tuesday", "wednesday", "thursday", "friday"]


def test_biweekly_uses_interval_two():
    pattern = build_recurrence("biweekly", anchor=NOW)["pattern"]
    assert pattern["type"] == "weekly" and pattern["interval"] == 2


def test_monthly_defaults_to_the_anchor_day():
    pattern = build_recurrence("monthly", anchor=NOW)["pattern"]
    assert pattern == {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": 19}


def test_monthly_with_explicit_day():
    assert build_recurrence("monthly:15", anchor=NOW)["pattern"]["dayOfMonth"] == 15


def test_yearly_carries_month_and_day():
    pattern = build_recurrence("yearly", anchor=NOW)["pattern"]
    assert pattern == {"type": "absoluteYearly", "interval": 1, "dayOfMonth": 19, "month": 8}


def test_recurrence_end_date():
    result = build_recurrence("daily", anchor=NOW, until=dt.datetime(2026, 12, 31))
    assert result["range"] == {"type": "endDate", "startDate": "2026-08-19", "endDate": "2026-12-31"}


def test_recurrence_occurrence_count():
    result = build_recurrence("daily", anchor=NOW, count=10)
    assert result["range"] == {"type": "numbered", "startDate": "2026-08-19", "numberOfOccurrences": 10}


def test_end_date_and_count_are_mutually_exclusive():
    with pytest.raises(UsageError, match="not both"):
        build_recurrence("daily", anchor=NOW, until=dt.datetime(2026, 12, 31), count=3)


@pytest.mark.parametrize("spec", ["hourly", "daily:0", "daily:x", "monthly:40"])
def test_invalid_recurrence_specs(spec):
    with pytest.raises(UsageError):
        build_recurrence(spec, anchor=NOW)


def builder(**kw):
    return TaskPayloadBuilder(tz_name="UTC", now=NOW, **kw)


def test_untouched_fields_are_absent_from_the_payload():
    payload = builder().title("Thing").build()
    assert payload == {"title": "Thing"}


def test_clear_emits_an_explicit_null():
    payload = builder().title("Thing").clear("dueDateTime").build()
    assert payload["dueDateTime"] is None


def test_reminder_without_a_time_defaults_to_nine_am():
    payload = builder().reminder("tomorrow").build()
    assert payload["reminderDateTime"]["dateTime"].startswith("2026-08-20T09:00:00")
    assert payload["isReminderOn"] is True


def test_reminder_keeps_an_explicit_time():
    payload = builder().reminder("tomorrow 6pm").build()
    assert payload["reminderDateTime"]["dateTime"].startswith("2026-08-20T18:00:00")


def test_due_uses_utc_midnight_by_default():
    payload = builder().due("tomorrow").build()
    assert payload["dueDateTime"] == {"dateTime": "2026-08-20T00:00:00.0000000", "timeZone": "UTC"}


def test_categories_accept_repeats_and_commas():
    payload = builder().categories(["work,urgent", "home"]).build()
    assert payload["categories"] == ["work", "urgent", "home"]


def test_body_is_wrapped_as_item_body():
    payload = builder().note("some detail").build()
    assert payload["body"] == {"content": "some detail", "contentType": "text"}


def test_empty_title_is_rejected():
    with pytest.raises(UsageError, match="cannot be empty"):
        builder().title("   ")


def test_recurrence_anchors_on_the_due_date():
    payload = builder().due("2026-09-15").recurrence("monthly").build()
    assert payload["recurrence"]["pattern"]["dayOfMonth"] == 15
    assert payload["recurrence"]["range"]["startDate"] == "2026-09-15"
