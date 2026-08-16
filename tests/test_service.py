import datetime as dt

import pytest

from mstodo.auth import DeviceCodeAuth
from mstodo.config import Config
from mstodo.errors import AmbiguousReferenceError, NotFoundError, UsageError
from mstodo.graph import GraphClient
from mstodo.http import RetryPolicy
from mstodo.service import TodoService, filter_tasks, task_sort_key

LISTS = [
    {"id": "list-default", "displayName": "Tasks", "wellknownListName": "defaultList", "isOwner": True},
    {"id": "list-flagged", "displayName": "Flagged email", "wellknownListName": "flaggedEmails"},
    {"id": "list-work", "displayName": "Work Projects", "wellknownListName": "none"},
    {"id": "list-home", "displayName": "Home", "wellknownListName": "none"},
    {"id": "list-work2", "displayName": "Work Errands", "wellknownListName": "none"},
]


def make_service(transport, signed_in, **kw):
    signed_in()
    config = Config()
    auth = DeviceCodeAuth(config, opener=transport.opener)
    client = GraphClient(
        auth,
        config,
        policy=RetryPolicy(attempts=1, sleep=lambda s: None),
        opener=transport.opener,
        **kw,
    )
    return TodoService(client, use_cache=False)


def stub_lists(transport, lists=None):
    transport.json("GET", "/me/todo/lists", {"value": lists if lists is not None else LISTS}, repeat=True)


# ------------------------------------------------------------------ lists


def test_no_reference_resolves_to_the_built_in_tasks_list(transport, signed_in):
    service = make_service(transport, signed_in)
    stub_lists(transport)
    assert service.resolve_list(None)["id"] == "list-default"


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("list-work", "list-work"),
        ("Home", "list-home"),
        ("home", "list-home"),
        ("default", "list-default"),
        ("tasks", "list-default"),
        ("flagged", "list-flagged"),
        ("Projects", "list-work"),
    ],
)
def test_list_resolution_by_id_name_alias_and_substring(transport, signed_in, ref, expected):
    service = make_service(transport, signed_in)
    stub_lists(transport)
    assert service.resolve_list(ref)["id"] == expected


def test_ambiguous_list_names_list_the_candidates(transport, signed_in):
    service = make_service(transport, signed_in)
    stub_lists(transport)

    with pytest.raises(AmbiguousReferenceError) as excinfo:
        service.resolve_list("Work")

    assert "Work Projects" in (excinfo.value.hint or "")
    assert "Work Errands" in (excinfo.value.hint or "")


def test_unknown_list_raises_not_found(transport, signed_in):
    service = make_service(transport, signed_in)
    stub_lists(transport)
    with pytest.raises(NotFoundError, match="no task list matches"):
        service.resolve_list("Nonexistent")


def test_empty_mailbox_raises_a_clear_error(transport, signed_in):
    service = make_service(transport, signed_in)
    stub_lists(transport, [])
    with pytest.raises(NotFoundError, match="no task lists found"):
        service.resolve_list(None)


# ------------------------------------------------------------------ tasks

TASKS = [
    {"id": "task-aaaaaaaaaaaaaaaaaaaa1", "title": "Buy milk", "status": "notStarted"},
    {"id": "task-bbbbbbbbbbbbbbbbbbbb2", "title": "Buy bread", "status": "notStarted"},
    {"id": "task-ccccccccccccccccccc3", "title": "Call the bank", "status": "completed"},
]


def stub_tasks(transport, tasks=None):
    transport.json(
        "GET", "/me/todo/lists/list-work/tasks", {"value": tasks if tasks is not None else TASKS}, repeat=True
    )


def test_task_resolution_by_exact_title(transport, signed_in):
    service = make_service(transport, signed_in)
    stub_tasks(transport)
    assert service.resolve_task("list-work", "Buy milk")["id"].startswith("task-a")


def test_task_resolution_by_unique_substring(transport, signed_in):
    service = make_service(transport, signed_in)
    stub_tasks(transport)
    assert service.resolve_task("list-work", "bank")["id"].startswith("task-c")


def test_ambiguous_task_title_raises(transport, signed_in):
    service = make_service(transport, signed_in)
    stub_tasks(transport)
    with pytest.raises(AmbiguousReferenceError, match="matches 2 tasks"):
        service.resolve_task("list-work", "Buy")


def test_task_resolution_by_id_fragment(transport, signed_in):
    service = make_service(transport, signed_in)
    stub_tasks(transport)
    assert service.resolve_task("list-work", "task-b")["title"] == "Buy bread"


def test_empty_task_reference_is_rejected(transport, signed_in):
    service = make_service(transport, signed_in)
    with pytest.raises(UsageError, match="empty task reference"):
        service.resolve_task("list-work", "  ")


def test_create_task_requires_a_title(transport, signed_in):
    service = make_service(transport, signed_in)
    with pytest.raises(UsageError, match="needs a title"):
        service.create_task("list-work", {"importance": "high"})


def test_update_with_no_fields_is_rejected(transport, signed_in):
    service = make_service(transport, signed_in)
    with pytest.raises(UsageError, match="nothing to update"):
        service.update_task("list-work", "task-1", {})


def test_move_copies_children_then_deletes_the_original(transport, signed_in):
    service = make_service(transport, signed_in)
    task = {
        "id": "task-1",
        "title": "Migrate me",
        "importance": "high",
        "status": "notStarted",
        "dueDateTime": {"dateTime": "2026-08-20T00:00:00.0000000", "timeZone": "UTC"},
    }
    transport.json("POST", "/list-home/tasks", {"id": "task-new", "title": "Migrate me"})
    transport.json("GET", "/list-work/tasks/task-1/checklistItems", {"value": [{"displayName": "step one", "isChecked": True}]})
    transport.json("POST", "/list-home/tasks/task-new/checklistItems", {"id": "c1"})
    transport.json("GET", "/list-work/tasks/task-1/linkedResources", {"value": [{"displayName": "src", "applicationName": "app", "webUrl": "https://x"}]})
    transport.json("POST", "/list-home/tasks/task-new/linkedResources", {"id": "r1"})
    transport.add("DELETE", "/list-work/tasks/task-1", status=204)

    result = service.move_task("list-work", task, "list-home")

    assert result["id"] == "task-new"
    created = transport.requests_to("/list-home/tasks")[0]["body"]
    assert created["importance"] == "high"
    assert created["dueDateTime"]["timeZone"] == "UTC"
    checklist = transport.requests_to("/task-new/checklistItems")[0]["body"]
    assert checklist == {"displayName": "step one", "isChecked": True}
    assert any(c["method"] == "DELETE" for c in transport.calls)


def test_attachment_under_three_mb_is_sent_inline(transport, signed_in, tmp_path):
    service = make_service(transport, signed_in)
    path = tmp_path / "note.txt"
    path.write_bytes(b"hello world")
    transport.json("POST", "/tasks/task-1/attachments", {"id": "att-1"})

    service.add_attachment("list-work", "task-1", path)

    body = transport.calls[0]["body"]
    assert body["@odata.type"] == "#microsoft.graph.taskFileAttachment"
    assert body["name"] == "note.txt"
    assert body["size"] == 11
    assert "createUploadSession" not in transport.calls[0]["url"]


def test_attachment_over_three_mb_uses_an_upload_session(transport, signed_in, tmp_path):
    service = make_service(transport, signed_in)
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (4 * 1024 * 1024))
    transport.json("POST", "/attachments/createUploadSession", {"uploadUrl": "https://upload.test/session"})
    transport.json("PUT", "https://upload.test/session", {"id": "att-big"}, repeat=True)

    result = service.add_attachment("list-work", "task-1", path)

    assert result["id"] == "att-big"
    uploads = transport.requests_to("upload.test")
    assert len(uploads) == 2  # 4 MiB split into 3.125 MiB chunks
    assert uploads[0]["headers"]["Content-range"].startswith("bytes 0-")
    # The pre-authenticated upload URL must not carry our bearer token.
    assert "Authorization" not in uploads[0]["headers"]


def test_missing_attachment_file_is_a_usage_error(transport, signed_in, tmp_path):
    service = make_service(transport, signed_in)
    with pytest.raises(UsageError, match="not a file"):
        service.add_attachment("list-work", "task-1", tmp_path / "nope.txt")


# ---------------------------------------------------------------- filtering

NOW = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.timezone.utc)


def task(**kw):
    base = {"id": "x", "title": "t", "status": "notStarted", "importance": "normal"}
    base.update(kw)
    return base


def due(day: int):
    return {"dateTime": f"2026-08-{day:02d}T00:00:00.0000000", "timeZone": "UTC"}


def test_completed_tasks_are_hidden_by_default():
    tasks = [task(status="completed"), task(status="notStarted")]
    assert len(filter_tasks(tasks)) == 1


def test_include_completed_keeps_them():
    tasks = [task(status="completed"), task(status="notStarted")]
    assert len(filter_tasks(tasks, include_completed=True)) == 2


def test_explicit_status_filter_overrides_the_default_hiding():
    tasks = [task(status="completed"), task(status="notStarted")]
    result = filter_tasks(tasks, status="completed")
    assert len(result) == 1 and result[0]["status"] == "completed"


def test_due_before_excludes_tasks_without_a_due_date():
    tasks = [task(dueDateTime=due(18)), task()]
    assert len(filter_tasks(tasks, due_before=NOW)) == 1


def test_due_window():
    tasks = [task(dueDateTime=due(18)), task(dueDateTime=due(20)), task(dueDateTime=due(25))]
    result = filter_tasks(
        tasks, due_after=NOW - dt.timedelta(days=2), due_before=NOW + dt.timedelta(days=3)
    )
    assert len(result) == 2


def test_no_due_filter():
    tasks = [task(dueDateTime=due(20)), task()]
    result = filter_tasks(tasks, has_due=False)
    assert len(result) == 1 and "dueDateTime" not in result[0]


def test_category_filter_is_case_insensitive():
    tasks = [task(categories=["Work"]), task(categories=["Home"])]
    assert len(filter_tasks(tasks, categories=["work"])) == 1


def test_search_covers_title_body_and_categories():
    tasks = [
        task(title="Pay invoice"),
        task(body={"content": "remember the invoice number", "contentType": "text"}),
        task(categories=["invoicing"]),
        task(title="Unrelated"),
    ]
    assert len(filter_tasks(tasks, query="invoic")) == 3


def test_sort_key_pushes_missing_due_dates_last():
    with_due = task(dueDateTime=due(20))
    without = task()
    ordered = sorted([without, with_due], key=lambda t: task_sort_key(t, "due"))
    assert ordered[0] is with_due


def test_importance_sort_order():
    tasks = [task(importance="low"), task(importance="high"), task(importance="normal")]
    ordered = sorted(tasks, key=lambda t: task_sort_key(t, "importance"))
    assert [t["importance"] for t in ordered] == ["high", "normal", "low"]


# ------------------------------------------------ Outlook ids share a prefix

# Real Outlook ids look like this: a long mailbox-derived head that is IDENTICAL
# across every list and task in the account, with the entropy at the tail.
OUTLOOK_HEAD = "AQMkADAwATM0MDAAMS0yMDkyLWVjMzYtMDACLTAwCgBGAAAD"
OUTLOOK_TASKS = [
    {"id": OUTLOOK_HEAD + "xlnrYAAA=", "title": "Take protein", "status": "notStarted"},
    {"id": OUTLOOK_HEAD + "qp8vLl2BBB=", "title": "Learn French", "status": "notStarted"},
    {"id": OUTLOOK_HEAD + "wm4xPp1CCC=", "title": "Vacuum home", "status": "notStarted"},
]


def test_id_prefix_would_be_ambiguous_so_fragments_are_used(transport, signed_in):
    """Truncating an Outlook id from the front yields the same string for every
    task, so resolution must match anywhere in the id, not just at the start."""
    service = make_service(transport, signed_in)
    transport.json("GET", "/me/todo/lists/list-work/tasks", {"value": OUTLOOK_TASKS}, repeat=True)

    assert service.resolve_task("list-work", "xlnrYAAA=")["title"] == "Take protein"
    assert service.resolve_task("list-work", "qp8vLl2BBB=")["title"] == "Learn French"


def test_the_shared_head_is_reported_as_ambiguous_not_silently_wrong(transport, signed_in):
    service = make_service(transport, signed_in)
    transport.json("GET", "/me/todo/lists/list-work/tasks", {"value": OUTLOOK_TASKS}, repeat=True)

    with pytest.raises(AmbiguousReferenceError, match="matches 3 tasks"):
        service.resolve_task("list-work", OUTLOOK_HEAD[:12])


def test_displayed_id_tail_round_trips_back_into_resolution(transport, signed_in):
    """Whatever `ls` prints must be paste-able straight back in."""
    from mstodo.format import short_id

    service = make_service(transport, signed_in)
    transport.json("GET", "/me/todo/lists/list-work/tasks", {"value": OUTLOOK_TASKS}, repeat=True)

    for task in OUTLOOK_TASKS:
        displayed = short_id(task["id"])
        assert service.resolve_task("list-work", displayed)["id"] == task["id"]


def test_an_ellipsis_pasted_along_with_the_id_is_tolerated(transport, signed_in):
    service = make_service(transport, signed_in)
    transport.json("GET", "/me/todo/lists/list-work/tasks", {"value": OUTLOOK_TASKS}, repeat=True)

    assert service.resolve_task("list-work", "…xlnrYAAA=")["title"] == "Take protein"


def test_lists_resolve_by_id_fragment_but_names_win(transport, signed_in):
    service = make_service(transport, signed_in)
    outlook_lists = [
        {"id": OUTLOOK_HEAD + "gDbc8U7HGwAA=", "displayName": "Tasks", "wellknownListName": "defaultList"},
        {"id": OUTLOOK_HEAD + "zPnuBwDit9AA=", "displayName": "Revise", "wellknownListName": "none"},
    ]
    stub_lists(transport, outlook_lists)

    assert service.resolve_list("zPnuBwDit9AA=")["displayName"] == "Revise"
    assert service.resolve_list("Revise")["displayName"] == "Revise"
    with pytest.raises(AmbiguousReferenceError, match="matches 2 lists"):
        service.resolve_list(OUTLOOK_HEAD[:20])
