import json

import pytest

from mstodo.cli import build_parser, main

LISTS = {
    "value": [
        {"id": "list-default", "displayName": "Tasks", "wellknownListName": "defaultList"},
        {"id": "list-work", "displayName": "Work", "wellknownListName": "none"},
    ]
}

TASKS = {
    "value": [
        {
            "id": "task-1111111111111111111",
            "title": "Overdue thing",
            "status": "notStarted",
            "importance": "high",
            "dueDateTime": {"dateTime": "2020-01-01T00:00:00.0000000", "timeZone": "UTC"},
        },
        {
            "id": "task-2222222222222222222",
            "title": "Someday thing",
            "status": "notStarted",
            "importance": "normal",
        },
        {
            "id": "task-3333333333333333333",
            "title": "Finished thing",
            "status": "completed",
        },
    ]
}


def run(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def graph(transport, signed_in):
    signed_in()
    transport.json("GET", "/me/todo/lists", LISTS, repeat=True)
    transport.json("GET", "/me/todo/lists/list-default/tasks", TASKS, repeat=True)
    transport.json("GET", "/me/todo/lists/list-work/tasks", {"value": []}, repeat=True)
    return transport


# ------------------------------------------------------------------ basics


def test_help_exits_zero():
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0


def test_version_exits_zero():
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_no_command_prints_help_and_returns_two(capsys):
    code, out, _ = run([], capsys)
    assert code == 2
    assert "usage:" in out


def test_auth_status_when_signed_out_returns_three(capsys):
    code, out, _ = run(["auth", "status"], capsys)
    assert code == 3
    assert "not signed in" in out


def test_auth_status_when_signed_in(capsys, signed_in):
    signed_in()
    code, out, _ = run(["auth", "status"], capsys)
    assert code == 0
    assert "tester@example.com" in out


def test_login_without_a_client_id_is_a_config_error(capsys, monkeypatch):
    monkeypatch.delenv("MSTODO_CLIENT_ID", raising=False)
    code, _, err = run(["auth", "login"], capsys)
    assert code == 2
    assert "no Microsoft Entra client id" in err
    assert "setup.md" in err


# ------------------------------------------------------------------- lists


def test_lists_ls_table(capsys, graph):
    code, out, _ = run(["--no-cache", "lists", "ls"], capsys)
    assert code == 0
    assert "Tasks" in out and "defaultList" in out
    assert "Work" in out


def test_lists_ls_json_is_the_raw_graph_objects(capsys, graph):
    code, out, _ = run(["--no-cache", "--json", "lists", "ls"], capsys)
    assert code == 0
    assert json.loads(out) == LISTS["value"]


def test_lists_new(capsys, graph):
    graph.json("POST", "/me/todo/lists", {"id": "list-new", "displayName": "Groceries"})
    code, out, _ = run(["--no-cache", "lists", "new", "Groceries"], capsys)
    assert code == 0
    assert "created list 'Groceries'" in out
    assert graph.requests_to("/me/todo/lists")[-1]["body"] == {"displayName": "Groceries"}


def test_built_in_lists_cannot_be_deleted(capsys, graph):
    code, _, err = run(["--no-cache", "-y", "lists", "rm", "Tasks"], capsys)
    assert code == 2
    assert "built-in list" in err


# ------------------------------------------------------------------- tasks


def test_ls_hides_completed_by_default(capsys, graph):
    code, out, _ = run(["--no-cache", "ls"], capsys)
    assert code == 0
    assert "Overdue thing" in out
    assert "Finished thing" not in out


def test_ls_include_completed(capsys, graph):
    _, out, _ = run(["--no-cache", "ls", "--include-completed"], capsys)
    assert "Finished thing" in out


def test_ls_overdue_filter(capsys, graph):
    code, out, _ = run(["--no-cache", "--json", "ls", "--overdue"], capsys)
    assert code == 0
    titles = [t["title"] for t in json.loads(out)]
    assert titles == ["Overdue thing"]


def test_ls_no_due_filter(capsys, graph):
    _, out, _ = run(["--no-cache", "--json", "ls", "--no-due"], capsys)
    titles = [t["title"] for t in json.loads(out)]
    assert titles == ["Someday thing"]


def test_ls_all_lists_tags_each_task_with_its_list(capsys, graph):
    code, out, _ = run(["--no-cache", "--json", "ls", "--all-lists"], capsys)
    assert code == 0
    assert {t["_listName"] for t in json.loads(out)} == {"Tasks"}


def test_ls_search(capsys, graph):
    _, out, _ = run(["--no-cache", "--json", "ls", "-s", "someday"], capsys)
    assert [t["title"] for t in json.loads(out)] == ["Someday thing"]


def test_ls_jsonl_emits_one_object_per_line(capsys, graph):
    _, out, _ = run(["--no-cache", "--jsonl", "ls"], capsys)
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 2
    assert all(json.loads(line)["id"] for line in lines)


def test_add_sends_the_expected_payload(capsys, graph):
    graph.json("POST", "/me/todo/lists/list-default/tasks", {"id": "task-new", "title": "Renew passport"})
    code, out, _ = run(
        [
            "--no-cache", "--tz", "UTC",
            "add", "Renew", "passport",
            "--due", "2026-09-01",
            "--importance", "high",
            "-c", "admin,personal",
            "-n", "book an appointment",
        ],
        capsys,
    )
    assert code == 0
    body = graph.requests_to("/list-default/tasks")[-1]["body"]
    assert body["title"] == "Renew passport"
    assert body["importance"] == "high"
    assert body["categories"] == ["admin", "personal"]
    assert body["body"] == {"content": "book an appointment", "contentType": "text"}
    assert body["dueDateTime"] == {"dateTime": "2026-09-01T00:00:00.0000000", "timeZone": "UTC"}
    assert "added" in out


def test_add_with_recurrence(capsys, graph):
    graph.json("POST", "/me/todo/lists/list-default/tasks", {"id": "task-new", "title": "Water plants"})
    code, _, _ = run(
        ["--no-cache", "--tz", "UTC", "add", "Water plants", "--due", "2026-09-02", "--recur", "weekly:mon,thu"],
        capsys,
    )
    assert code == 0
    recurrence = graph.requests_to("/list-default/tasks")[-1]["body"]["recurrence"]
    assert recurrence["pattern"]["daysOfWeek"] == ["monday", "thursday"]
    assert recurrence["range"]["startDate"] == "2026-09-02"


def test_add_with_checklist_items(capsys, graph):
    graph.json("POST", "/me/todo/lists/list-default/tasks", {"id": "task-new", "title": "Trip"})
    graph.json("POST", "/tasks/task-new/checklistItems", {"id": "c1"}, repeat=True)
    code, _, _ = run(["--no-cache", "add", "Trip", "--checklist", "book flight", "--checklist", "pack"], capsys)
    assert code == 0
    assert len(graph.requests_to("/checklistItems")) == 2


def test_add_if_not_exists_is_a_no_op_when_present(capsys, graph):
    code, out, _ = run(["--no-cache", "add", "Someday thing", "--if-not-exists"], capsys)
    assert code == 0
    assert "already exists" in out
    assert not [c for c in graph.calls if c["method"] == "POST"]


def test_dry_run_sends_no_writes(capsys, graph):
    code, out, _ = run(["--no-cache", "--dry-run", "add", "Nothing real"], capsys)
    assert code == 0
    assert "dry-run" in out
    assert not [c for c in graph.calls if c["method"] == "POST"]


def test_done_resolves_by_title_and_patches_status(capsys, graph):
    graph.json("PATCH", "/tasks/task-2222222222222222222", {"id": "task-2222222222222222222", "status": "completed"})
    code, out, _ = run(["--no-cache", "done", "Someday"], capsys)
    assert code == 0
    patch = next(c for c in graph.calls if c["method"] == "PATCH")
    assert patch["body"] == {"status": "completed"}
    assert "completed" in out


def test_update_clear_due_sends_an_explicit_null(capsys, graph):
    graph.json("PATCH", "/tasks/task-1111111111111111111", {"id": "task-1111111111111111111"})
    code, _, _ = run(["--no-cache", "update", "Overdue", "--clear-due"], capsys)
    assert code == 0
    patch = next(c for c in graph.calls if c["method"] == "PATCH")
    assert patch["body"] == {"dueDateTime": None}


def test_update_with_no_fields_is_a_usage_error(capsys, graph):
    code, _, err = run(["--no-cache", "update", "Overdue"], capsys)
    assert code == 2
    assert "nothing to update" in err


def test_ambiguous_reference_lists_candidates(capsys, graph):
    # Reference resolution deliberately searches completed tasks too, so all
    # three "... thing" tasks are candidates here.
    code, _, err = run(["--no-cache", "done", "thing"], capsys)
    assert code == 2
    assert "matches 3 tasks" in err
    assert "Overdue thing" in err
    assert "Finished thing" in err


def test_unknown_task_returns_exit_code_four(capsys, graph):
    code, _, err = run(["--no-cache", "done", "nonexistent"], capsys)
    assert code == 4
    assert "no task matches" in err


def test_delete_without_confirmation_refuses_non_interactively(capsys, graph):
    code, _, err = run(["--no-cache", "rm", "Overdue"], capsys)
    assert code == 2
    assert "--yes" in err
    assert not [c for c in graph.calls if c["method"] == "DELETE"]


def test_delete_with_yes_proceeds(capsys, graph):
    graph.add("DELETE", "/tasks/task-1111111111111111111", status=204)
    code, _, _ = run(["--no-cache", "-y", "rm", "Overdue"], capsys)
    assert code == 0
    assert [c for c in graph.calls if c["method"] == "DELETE"]


def test_move_copies_then_deletes(capsys, graph):
    graph.json("POST", "/list-work/tasks", {"id": "task-moved", "title": "Overdue thing"})
    graph.json("GET", "/task-1111111111111111111/checklistItems", {"value": []})
    graph.json("GET", "/task-1111111111111111111/linkedResources", {"value": []})
    graph.add("DELETE", "/list-default/tasks/task-1111111111111111111", status=204)

    code, out, _ = run(["--no-cache", "move", "Overdue", "--to", "Work"], capsys)

    assert code == 0
    assert "moved" in out
    assert [c for c in graph.calls if c["method"] == "DELETE"]


def test_move_to_the_same_list_is_rejected(capsys, graph):
    code, _, err = run(["--no-cache", "move", "Overdue", "--to", "Tasks"], capsys)
    assert code == 2
    assert "same" in err


# ------------------------------------------------------------------- delta


def test_delta_stores_and_reuses_the_delta_link(capsys, graph, isolated_config):
    graph.json(
        "GET",
        "/tasks/delta",
        {"value": [{"id": "task-1", "title": "New", "status": "notStarted"}],
         "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta?token=abc"},
    )
    code, out, _ = run(["--no-cache", "--json", "delta"], capsys)
    assert code == 0
    assert json.loads(out)["changed"] == 1

    state = json.loads((isolated_config / "delta-state.json").read_text())
    assert state["list-default"].endswith("token=abc")

    graph.json("GET", "/delta?token=abc", {"value": [], "@odata.deltaLink": "https://x/delta?token=def"})
    code, out, _ = run(["--no-cache", "--json", "delta"], capsys)
    assert json.loads(out)["changed"] == 0


def test_delta_reports_removals_separately(capsys, graph):
    graph.json(
        "GET",
        "/tasks/delta",
        {"value": [{"id": "gone", "@removed": {"reason": "deleted"}}],
         "@odata.deltaLink": "https://x/delta?token=z"},
    )
    _, out, _ = run(["--no-cache", "--json", "delta"], capsys)
    payload = json.loads(out)
    assert payload["removed"] == 1
    assert payload["removedIds"] == ["gone"]


# --------------------------------------------------------------------- raw


def test_raw_passes_through_to_graph(capsys, graph):
    code, out, _ = run(["--no-cache", "raw", "GET", "/me/todo/lists"], capsys)
    assert code == 0
    assert json.loads(out) == LISTS


def test_raw_rejects_invalid_json_bodies(capsys, graph):
    code, _, err = run(["--no-cache", "raw", "POST", "/me/todo/lists", "--data", "{nope}"], capsys)
    assert code == 2
    assert "not valid JSON" in err


# ------------------------------------------------------------- error paths


def test_throttling_surfaces_exit_code_five(capsys, transport, signed_in):
    signed_in()
    transport.add(
        "GET", "/me/todo/lists", status=429, repeat=True,
        json_body={"error": {"code": "TooManyRequests", "message": "slow down"}},
        headers={"Retry-After": "0"},
    )
    code, _, err = run(["--no-cache", "--retries", "1", "lists", "ls"], capsys)
    assert code == 5
    assert "throttl" in err.lower()


def test_missing_scope_surfaces_exit_code_six(capsys, transport, signed_in):
    signed_in()
    transport.add(
        "GET", "/me/todo/lists", status=403, repeat=True,
        json_body={"error": {"code": "ErrorAccessDenied", "message": "Access is denied"}},
    )
    code, _, err = run(["--no-cache", "--retries", "1", "lists", "ls"], capsys)
    assert code == 6
    assert "Tasks.ReadWrite" in err


def test_bad_timezone_fails_before_any_request(capsys, transport, signed_in):
    signed_in()
    code, _, err = run(["--tz", "Nowhere/Special", "ls"], capsys)
    assert code == 2
    assert "unknown time zone" in err
    assert transport.calls == []


# ------------------------------------------- credential flags on `auth login`

def test_client_id_and_tenant_accepted_after_the_subcommand(capsys, monkeypatch):
    """Regression: every doc example writes `auth login --client-id X`, but the
    flags were originally global-only, so that exact form failed with exit 2."""
    parser = build_parser()
    args = parser.parse_args(
        ["auth", "login", "--client-id", "abc-123", "--tenant", "consumers", "--save"]
    )
    assert args.client_id == "abc-123"
    assert args.tenant == "consumers"
    assert args.save is True


def test_client_id_still_accepted_before_the_subcommand():
    parser = build_parser()
    args = parser.parse_args(["--client-id", "abc-123", "auth", "login"])
    assert args.client_id == "abc-123"


def test_omitted_subcommand_flag_does_not_clobber_the_global_one():
    """argparse.SUPPRESS is what makes this work; without it the subparser
    default of None would overwrite the value parsed before the subcommand."""
    parser = build_parser()
    args = parser.parse_args(["--client-id", "global-id", "--tenant", "common", "auth", "login"])
    assert args.client_id == "global-id"
    assert args.tenant == "common"


def test_subcommand_flag_wins_over_the_global_one():
    parser = build_parser()
    args = parser.parse_args(
        ["--client-id", "global-id", "auth", "login", "--client-id", "specific-id"]
    )
    assert args.client_id == "specific-id"
