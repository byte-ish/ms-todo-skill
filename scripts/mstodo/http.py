"""Thin HTTP layer over urllib with retry, backoff and Retry-After handling.

Kept deliberately free of Graph or OAuth specifics so both the token endpoint
and the Graph endpoint can share one retry policy — and so the tests can drive
it without a network.
"""

from __future__ import annotations

import email.utils
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

from . import USER_AGENT

log = logging.getLogger("mstodo.http")

#: Status codes worth trying again. 429 is throttling; the 5xx set is transient
#: by definition. 408 shows up occasionally from front-door proxies.
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

MAX_BACKOFF_SECONDS = 60.0
#: Never honour an absurd Retry-After — a bad gateway can return a huge value
#: and we would rather fail loudly than hang a user's terminal for an hour.
MAX_RETRY_AFTER_SECONDS = 120.0


@dataclass
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    def header(self, name: str) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None


@dataclass
class RetryPolicy:
    attempts: int = 5
    base_delay: float = 0.5
    jitter: float = 0.25
    sleep: Callable[[float], None] = field(default=time.sleep)
    rand: Callable[[], float] = field(default=random.random)

    def backoff(self, attempt: int) -> float:
        """Full exponential backoff for a zero-based attempt index."""
        delay = min(self.base_delay * (2**attempt), MAX_BACKOFF_SECONDS)
        return delay + (self.rand() * self.jitter * delay)


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Return seconds to wait, accepting both delta-seconds and HTTP-date forms."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        # Raises on malformed input in 3.10+, returns None on older versions.
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    reference = time.time() if now is None else now
    return max(0.0, parsed.timestamp() - reference)


def build_url(base: str, path: str, params: Mapping[str, Any] | None = None) -> str:
    """Join ``base`` and ``path`` and append query params.

    An absolute ``path`` wins outright, which is what makes following Graph's
    ``@odata.nextLink`` and ``@odata.deltaLink`` URLs a one-liner.
    """
    url = path if path.startswith("http://") or path.startswith("https://") else f"{base}/{path.lstrip('/')}"
    if params:
        pairs = [(k, v) for k, v in params.items() if v is not None]
        if pairs:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)
    return url


def request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30.0,
    policy: RetryPolicy | None = None,
    opener: Callable[..., Any] | None = None,
) -> Response:
    """Perform an HTTP request, retrying transient failures.

    Returns the final ``Response`` regardless of status — interpreting 4xx/5xx
    is the caller's job. Only network errors that survive every attempt raise.
    """
    policy = policy or RetryPolicy()
    opener = opener or urllib.request.urlopen
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})

    last_exc: Exception | None = None
    for attempt in range(policy.attempts):
        req = urllib.request.Request(url=url, data=body, method=method.upper())
        for key, value in hdrs.items():
            req.add_header(key, value)

        try:
            with opener(req, timeout=timeout) as resp:
                response = Response(
                    status=getattr(resp, "status", resp.getcode()),
                    headers=dict(resp.headers.items()),
                    body=resp.read(),
                    url=resp.geturl(),
                )
        except urllib.error.HTTPError as exc:
            response = Response(
                status=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=exc.read() if hasattr(exc, "read") else b"",
                url=url,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt == policy.attempts - 1:
                break
            delay = policy.backoff(attempt)
            log.debug("network error (%s); retrying in %.2fs", exc, delay)
            policy.sleep(delay)
            continue

        if response.status in RETRY_STATUSES and attempt < policy.attempts - 1:
            hinted = parse_retry_after(response.header("Retry-After"))
            delay = min(hinted, MAX_RETRY_AFTER_SECONDS) if hinted is not None else policy.backoff(attempt)
            log.debug("HTTP %s from %s; retrying in %.2fs", response.status, url, delay)
            policy.sleep(delay)
            continue

        return response

    raise ConnectionError(f"{method.upper()} {url} failed after {policy.attempts} attempts: {last_exc}")


def form_encode(payload: Mapping[str, Any]) -> bytes:
    return urllib.parse.urlencode({k: v for k, v in payload.items() if v is not None}).encode("utf-8")
