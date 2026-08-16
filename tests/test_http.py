import time

import pytest

from mstodo.http import RetryPolicy, build_url, form_encode, parse_retry_after, request


def policy(**kw):
    """Deterministic policy: no jitter, no real sleeping."""
    delays: list[float] = []
    defaults = {"attempts": 4, "base_delay": 0.5, "jitter": 0.0, "rand": lambda: 0.0, "sleep": delays.append}
    defaults.update(kw)
    p = RetryPolicy(**defaults)
    return p, delays


def test_build_url_joins_and_encodes():
    url = build_url("https://graph.microsoft.com/v1.0", "/me/todo/lists", {"$top": 5, "$filter": "a eq 'b'"})
    assert url.startswith("https://graph.microsoft.com/v1.0/me/todo/lists?")
    assert "%24top=5" in url
    assert "a%20eq%20%27b%27" in url


def test_build_url_passes_absolute_urls_through():
    """nextLink and deltaLink are absolute and must not be re-based."""
    absolute = "https://graph.microsoft.com/v1.0/me/todo/lists?$skiptoken=abc"
    assert build_url("https://example.invalid", absolute) == absolute


def test_build_url_drops_none_params():
    assert build_url("https://x/y", "/z", {"a": None, "b": 1}) == "https://x/y/z?b=1"


def test_build_url_appends_to_existing_query():
    url = build_url("https://x", "/y?already=1", {"b": 2})
    assert url == "https://x/y?already=1&b=2"


@pytest.mark.parametrize("value,expected", [("30", 30.0), ("0", 0.0), (None, None), ("garbage", None)])
def test_parse_retry_after_delta_seconds(value, expected):
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date():
    now = time.time()
    future = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(now + 60))
    seconds = parse_retry_after(future, now=now)
    assert seconds is not None and 55 <= seconds <= 65


def test_retries_on_429_and_honours_retry_after(transport):
    transport.add("GET", "/thing", status=429, headers={"Retry-After": "7"})
    transport.json("GET", "/thing", {"ok": True})
    p, delays = policy()

    resp = request("GET", "https://api.test/thing", policy=p, opener=transport.opener)

    assert resp.status == 200
    assert resp.json() == {"ok": True}
    assert delays == [7.0]


def test_retry_after_is_capped(transport):
    transport.add("GET", "/thing", status=503, headers={"Retry-After": "99999"})
    transport.json("GET", "/thing", {"ok": True})
    p, delays = policy()

    request("GET", "https://api.test/thing", policy=p, opener=transport.opener)

    assert delays == [120.0]  # MAX_RETRY_AFTER_SECONDS, not 99999


def test_exponential_backoff_without_retry_after(transport):
    for _ in range(3):
        transport.add("GET", "/thing", status=500)
    transport.json("GET", "/thing", {"ok": True})
    p, delays = policy()

    resp = request("GET", "https://api.test/thing", policy=p, opener=transport.opener)

    assert resp.status == 200
    assert delays == [0.5, 1.0, 2.0]


def test_gives_up_and_returns_the_last_error_response(transport):
    transport.add("GET", "/thing", status=503, repeat=True)
    p, _ = policy(attempts=3)

    resp = request("GET", "https://api.test/thing", policy=p, opener=transport.opener)

    assert resp.status == 503
    assert len(transport.calls) == 3


def test_404_is_not_retried(transport):
    transport.add("GET", "/thing", status=404, repeat=True)
    p, delays = policy()

    resp = request("GET", "https://api.test/thing", policy=p, opener=transport.opener)

    assert resp.status == 404
    assert delays == []
    assert len(transport.calls) == 1


def test_network_errors_are_retried_then_raise(transport, monkeypatch):
    calls = {"n": 0}

    def failing(req, timeout=None):
        calls["n"] += 1
        raise OSError("connection reset")

    p, delays = policy(attempts=3)
    with pytest.raises(ConnectionError, match="after 3 attempts"):
        request("GET", "https://api.test/thing", policy=p, opener=failing)

    assert calls["n"] == 3
    assert delays == [0.5, 1.0]


def test_user_agent_and_accept_are_always_sent(transport):
    transport.json("GET", "/thing", {})
    request("GET", "https://api.test/thing", opener=transport.opener)
    headers = transport.calls[0]["headers"]
    assert "ms-todo-skill" in headers["User-agent"]
    assert headers["Accept"] == "application/json"


def test_form_encode_skips_none():
    assert form_encode({"a": "1", "b": None, "c": "x y"}) == b"a=1&c=x+y"
