"""Microsoft Graph client: auth injection, error translation, paging.

Everything above this layer works in terms of dicts and typed errors; nothing
above it touches HTTP status codes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from typing import Any, Callable

from .auth import DeviceCodeAuth
from .config import Config
from .errors import (
    AuthError,
    GraphError,
    NotFoundError,
    PermissionError_,
    ThrottledError,
)
from .http import Response, RetryPolicy, build_url, request

log = logging.getLogger("mstodo.graph")

_SCOPE_HINT = (
    "the signed-in user or app registration is missing the delegated "
    "Tasks.ReadWrite scope; run: todo.py auth logout && todo.py auth login"
)


class GraphClient:
    """A minimal, well-behaved Microsoft Graph client."""

    def __init__(
        self,
        auth: DeviceCodeAuth,
        config: Config,
        *,
        timezone: str | None = None,
        dry_run: bool = False,
        policy: RetryPolicy | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.auth = auth
        self.config = config
        self.base = config.graph_base
        self.timezone = timezone
        self.dry_run = dry_run
        self.policy = policy or RetryPolicy(attempts=config.retries)
        self.opener = opener

    # -- plumbing ----------------------------------------------------------

    def _headers(self, extra: Mapping[str, str] | None, *, token: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}"}
        if self.timezone:
            # Makes Graph render dateTimeTimeZone values in the caller's zone
            # instead of UTC, so displayed due dates match the To Do app.
            headers["Prefer"] = f'outlook.timezone="{self.timezone}"'
        headers.update(extra or {})
        return headers

    def _raise_for_status(self, resp: Response, method: str, url: str) -> None:
        if resp.status < 400:
            return

        payload = resp.json()
        code = message = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            err = payload["error"]
            code = err.get("code")
            message = err.get("message")
        elif isinstance(payload, dict):
            code = payload.get("error")
            message = payload.get("error_description")
        message = (message or resp.text[:400] or f"{method} {url} failed").strip()

        request_id = resp.header("request-id") or resp.header("client-request-id")
        common = {"status": resp.status, "code": code, "request_id": request_id, "body": payload}

        if resp.status == 401:
            raise AuthError(message, hint="token rejected; run: todo.py auth login")
        if resp.status == 403:
            raise PermissionError_(message, hint=_SCOPE_HINT, **common)
        if resp.status == 404:
            raise NotFoundError(message, hint="check the list or task id", **common)
        if resp.status == 429:
            raise ThrottledError(
                message,
                hint="Graph is throttling this mailbox; retry later or lower concurrency",
                **common,
            )
        raise GraphError(message, **common)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Issue one Graph call and return the decoded body (``None`` for 204)."""
        method = method.upper()
        url = build_url(self.base, path, params)

        if self.dry_run and method != "GET":
            log.info("dry-run: %s %s %s", method, url, json.dumps(json_body) if json_body else "")
            return {"dryRun": True, "method": method, "url": url, "body": json_body}

        body = None
        extra = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            extra.setdefault("Content-Type", "application/json")

        token = self.auth.access_token()
        resp = request(
            method,
            url,
            headers=self._headers(extra, token=token),
            body=body,
            timeout=self.config.timeout,
            policy=self.policy,
            opener=self.opener,
        )

        # One forced refresh covers the case where the token was revoked or the
        # clock skewed; a second 401 is a real failure, not a stale token.
        if resp.status == 401:
            log.debug("401 from Graph; forcing a token refresh and retrying once")
            token = self.auth.access_token(force_refresh=True)
            resp = request(
                method,
                url,
                headers=self._headers(extra, token=token),
                body=body,
                timeout=self.config.timeout,
                policy=self.policy,
                opener=self.opener,
            )

        self._raise_for_status(resp, method, url)
        if resp.status == 204 or not resp.body:
            return None
        return resp.json()

    # -- verbs -------------------------------------------------------------

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, json_body=json_body, **kw)

    def patch(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        return self.request("PATCH", path, json_body=json_body, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)

    # -- paging ------------------------------------------------------------

    def paged(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield items across ``@odata.nextLink`` pages, stopping at ``limit``.

        Query parameters are sent once; Graph bakes them into the nextLink, so
        later pages must be fetched with the URL exactly as returned.
        """
        url: str | None = path
        first = True
        seen = 0
        while url:
            payload = self.get(url, params=params if first else None) or {}
            first = False
            for item in payload.get("value", []):
                yield item
                seen += 1
                if limit is not None and seen >= limit:
                    return
            url = payload.get("@odata.nextLink")

    def delta(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        page_size: int | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Walk a delta chain to completion.

        Returns every change in this round plus the ``@odata.deltaLink`` to hand
        back on the next call. Deleted items arrive as stubs carrying
        ``@removed``; callers must handle them explicitly.
        """
        headers = {"Prefer": f"odata.maxpagesize={page_size}"} if page_size else None
        url: str | None = path
        first = True
        items: list[dict[str, Any]] = []
        delta_link: str | None = None

        while url:
            payload = self.get(url, params=params if first else None, headers=headers) or {}
            first = False
            items.extend(payload.get("value", []))
            delta_link = payload.get("@odata.deltaLink") or delta_link
            url = payload.get("@odata.nextLink")

        return items, delta_link
