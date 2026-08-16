import pytest

from mstodo.auth import DeviceCodeAuth
from mstodo.config import Config
from mstodo.errors import AuthError, GraphError, NotFoundError, PermissionError_, ThrottledError
from mstodo.graph import GraphClient
from mstodo.http import RetryPolicy

GRAPH = "https://graph.microsoft.com/v1.0"


def make_client(transport, monkeypatch, signed_in, **kw):
    signed_in()
    config = Config()
    auth = DeviceCodeAuth(
        config,
        policy=RetryPolicy(attempts=1, sleep=lambda s: None),
        opener=transport.opener,
    )
    return GraphClient(
        auth,
        config,
        policy=RetryPolicy(attempts=2, jitter=0.0, rand=lambda: 0.0, sleep=lambda s: None),
        opener=transport.opener,
        **kw,
    )


def test_get_sends_bearer_token(transport, monkeypatch, signed_in):
    client = make_client(transport, monkeypatch, signed_in)
    transport.json("GET", "/me/todo/lists", {"value": []})

    client.get("/me/todo/lists")

    assert transport.calls[0]["headers"]["Authorization"] == "Bearer fake-access-token"


def test_timezone_preference_header_is_sent(transport, monkeypatch, signed_in):
    client = make_client(transport, monkeypatch, signed_in, timezone="Asia/Kolkata")
    transport.json("GET", "/me/todo/lists", {"value": []})

    client.get("/me/todo/lists")

    assert transport.calls[0]["headers"]["Prefer"] == 'outlook.timezone="Asia/Kolkata"'


@pytest.mark.parametrize(
    "status,expected",
    [
        (403, PermissionError_),
        (404, NotFoundError),
        (429, ThrottledError),
        (400, GraphError),
        (500, GraphError),
    ],
)
def test_status_codes_map_to_typed_errors(transport, monkeypatch, signed_in, status, expected):
    client = make_client(transport, monkeypatch, signed_in)
    transport.add(
        "GET",
        "/me/todo/lists",
        status=status,
        repeat=True,
        json_body={"error": {"code": "ErrorCode", "message": "it broke"}},
        headers={"request-id": "req-123"},
    )

    with pytest.raises(expected) as excinfo:
        client.get("/me/todo/lists")

    assert "it broke" in str(excinfo.value)
    assert excinfo.value.status == status
    assert excinfo.value.request_id == "req-123"


def test_permission_error_hint_names_the_missing_scope(transport, monkeypatch, signed_in):
    client = make_client(transport, monkeypatch, signed_in)
    transport.add("GET", "/me/todo/lists", status=403, json_body={"error": {"message": "denied"}}, repeat=True)

    with pytest.raises(PermissionError_) as excinfo:
        client.get("/me/todo/lists")

    assert "Tasks.ReadWrite" in (excinfo.value.hint or "")
    assert excinfo.value.exit_code == 6


def test_401_triggers_one_refresh_and_one_retry(transport, monkeypatch, signed_in):
    client = make_client(transport, monkeypatch, signed_in)
    transport.add("GET", "/me/todo/lists", status=401, json_body={"error": {"message": "expired"}})
    transport.json("POST", "/token", {"access_token": "ACCESS-2", "expires_in": 3600})
    transport.json("GET", "/me/todo/lists", {"value": [{"id": "1"}]})

    result = client.get("/me/todo/lists")

    assert result == {"value": [{"id": "1"}]}
    assert transport.calls[-1]["headers"]["Authorization"] == "Bearer ACCESS-2"


def test_second_401_is_a_real_auth_failure(transport, monkeypatch, signed_in):
    client = make_client(transport, monkeypatch, signed_in)
    transport.add("GET", "/me/todo/lists", status=401, json_body={"error": {"message": "revoked"}}, repeat=True)
    transport.json("POST", "/token", {"access_token": "ACCESS-2", "expires_in": 3600})

    with pytest.raises(AuthError, match="revoked"):
        client.get("/me/todo/lists")


def test_204_returns_none(transport, monkeypatch, signed_in):
    client = make_client(transport, monkeypatch, signed_in)
    transport.add("DELETE", "/tasks/abc", status=204)

    assert client.delete("/me/todo/lists/l1/tasks/abc") is None


def test_paged_follows_next_links(transport, monkeypatch, signed_in):
    client = make_client(transport, monkeypatch, signed_in)
    transport.json(
        "GET",
        "/me/todo/lists/l1/tasks",
        {"value": [{"id": "a"}, {"id": "b"}], "@odata.nextLink": f"{GRAPH}/page2"},
    )
    transport.json("GET", "/page2", {"value": [{"id": "c"}]})

    items = list(client.paged("/me/todo/lists/l1/tasks"))

    assert [i["id"] for i in items] == ["a", "b", "c"]


def test_paged_stops_at_limit_without_fetching_more(transport, monkeypatch, signed_in):
    client = make_client(transport, monkeypatch, signed_in)
    transport.json(
        "GET",
        "/me/todo/lists/l1/tasks",
        {"value": [{"id": "a"}, {"id": "b"}], "@odata.nextLink": f"{GRAPH}/page2"},
    )

    items = list(client.paged("/me/todo/lists/l1/tasks", limit=1))

    assert [i["id"] for i in items] == ["a"]
    assert len(transport.calls) == 1


def test_paged_sends_params_only_on_the_first_page(transport, monkeypatch, signed_in):
    client = make_client(transport, monkeypatch, signed_in)
    transport.json(
        "GET",
        "/me/todo/lists/l1/tasks",
        {"value": [{"id": "a"}], "@odata.nextLink": f"{GRAPH}/page2?%24skiptoken=x"},
    )
    transport.json("GET", "/page2", {"value": []})

    list(client.paged("/me/todo/lists/l1/tasks", params={"$top": 1}))

    assert "%24top=1" in transport.calls[0]["url"]
    assert "%24top=1" not in transport.calls[1]["url"]


def test_delta_walks_to_the_delta_link(transport, monkeypatch, signed_in):
    client = make_client(transport, monkeypatch, signed_in)
    transport.json(
        "GET",
        "/tasks/delta",
        {"value": [{"id": "a"}], "@odata.nextLink": f"{GRAPH}/delta-page2"},
    )
    transport.json(
        "GET",
        "/delta-page2",
        {"value": [{"id": "b", "@removed": {"reason": "deleted"}}], "@odata.deltaLink": f"{GRAPH}/delta?token=t2"},
    )

    changes, delta_link = client.delta("/me/todo/lists/l1/tasks/delta", page_size=50)

    assert [c["id"] for c in changes] == ["a", "b"]
    assert delta_link.endswith("token=t2")
    assert transport.calls[0]["headers"]["Prefer"] == "odata.maxpagesize=50"


def test_dry_run_suppresses_writes(transport, monkeypatch, signed_in):
    client = make_client(transport, monkeypatch, signed_in, dry_run=True)

    result = client.post("/me/todo/lists", {"displayName": "Nope"})

    assert result["dryRun"] is True
    assert transport.calls == []


def test_dry_run_still_allows_reads(transport, monkeypatch, signed_in):
    client = make_client(transport, monkeypatch, signed_in, dry_run=True)
    transport.json("GET", "/me/todo/lists", {"value": []})

    assert client.get("/me/todo/lists") == {"value": []}


def test_non_json_error_body_still_produces_a_useful_message(transport, monkeypatch, signed_in):
    client = make_client(transport, monkeypatch, signed_in)
    transport.add("GET", "/me/todo/lists", status=502, raw=b"<html>bad gateway</html>", repeat=True)

    with pytest.raises(GraphError, match="bad gateway"):
        client.get("/me/todo/lists")
