"""Shared fixtures: an in-memory HTTP stub and an isolated config directory.

No test in this suite touches the network or the real ``~/.config``.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class FakeHTTPResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str], url: str) -> None:
        self.status = status
        self._body = body
        self.headers = _Headers(headers)
        self._url = url

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Headers(dict):
    def items(self):
        return super().items()


class Route:
    """One queued reply, matched on method and a substring of the URL."""

    def __init__(
        self,
        method: str,
        match: str,
        *,
        status: int = 200,
        json_body: Any = None,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
        repeat: bool = False,
    ) -> None:
        self.method = method.upper()
        self.match = match
        self.status = status
        self.body = raw if raw is not None else (b"" if json_body is None else json.dumps(json_body).encode())
        self.headers = headers or {}
        self.repeat = repeat
        self.used = 0

    def matches(self, method: str, url: str) -> bool:
        """Match on the path *suffix*, not a bare substring.

        A substring match makes ``/me/todo/lists`` silently intercept
        ``/me/todo/lists/{id}/tasks``, which is a very easy way to write a test
        that passes for the wrong reason. Matches carrying a scheme or a query
        string fall back to whole-URL containment.
        """
        if method != self.method:
            return False
        if "://" in self.match or "?" in self.match:
            return self.match in url
        return urlparse(url).path.endswith(self.match)


class FakeTransport:
    """Records requests and replies from a queue of routes."""

    def __init__(self) -> None:
        self.routes: list[Route] = []
        self.calls: list[dict[str, Any]] = []

    def add(self, method: str, match: str, **kw: Any) -> FakeTransport:
        self.routes.append(Route(method, match, **kw))
        return self

    def json(self, method: str, match: str, payload: Any, **kw: Any) -> FakeTransport:
        return self.add(method, match, json_body=payload, **kw)

    def opener(self, req: Any, timeout: float | None = None) -> FakeHTTPResponse:
        method = req.get_method().upper()
        url = req.full_url
        body = req.data
        self.calls.append(
            {
                "method": method,
                "url": url,
                "path": urlparse(url).path,
                "body": json.loads(body) if body and body[:1] in (b"{", b"[") else body,
                "headers": dict(req.headers),
                "timeout": timeout,
            }
        )
        for route in self.routes:
            if route.matches(method, url) and (route.repeat or route.used == 0):
                route.used += 1
                if route.status >= 400:
                    raise urllib.error.HTTPError(
                        url, route.status, "error", _Headers(route.headers), _BytesIO(route.body)
                    )
                return FakeHTTPResponse(route.status, route.body, route.headers, url)
        raise AssertionError(f"unstubbed request: {method} {url}")

    def requests_to(self, fragment: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if fragment in c["url"]]


class _BytesIO:
    """Minimal file-like wrapper so HTTPError.read() and its cleanup both work."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("MSTODO_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("MSTODO_CLIENT_ID", raising=False)
    monkeypatch.delenv("MSTODO_TENANT", raising=False)
    monkeypatch.delenv("MSTODO_TIMEZONE", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TZ", "UTC")
    return config_dir


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    """Patch urlopen globally and hand back the recorder."""
    fake = FakeTransport()
    monkeypatch.setattr("urllib.request.urlopen", fake.opener)
    return fake


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make every backoff instantaneous while recording the delays requested."""
    recorded: list[float] = []

    def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr("time.sleep", fake_sleep)
    return recorded


@pytest.fixture
def signed_in(isolated_config: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Write a valid-looking token cache so Graph calls skip the login flow."""

    def _write(expires_in: float = 3600.0, client_id: str = "test-client", tenant: str = "common") -> None:
        import time as _time

        from mstodo.config import write_json_private

        write_json_private(
            isolated_config / "token.json",
            {
                "version": 1,
                "client_id": client_id,
                "tenant": tenant,
                "authority": "https://login.microsoftonline.com",
                "scope": "offline_access openid profile Tasks.ReadWrite",
                "access_token": "fake-access-token",
                "refresh_token": "fake-refresh-token",
                "expires_at": _time.time() + expires_in,
                "obtained_at": _time.time(),
                "account": {"username": "tester@example.com", "name": "Tester"},
            },
        )
        monkeypatch.setenv("MSTODO_CLIENT_ID", client_id)

    return _write
