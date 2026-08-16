"""OAuth 2.0 device authorization grant against the Microsoft identity platform.

Microsoft To Do exposes no application permissions — every write path in the API
is delegated-only — so a signed-in user is mandatory. The device code grant is
the flow that survives SSH sessions, containers and CI shells, because it needs
no redirect URI and no browser on the machine running the code.

Reference: https://learn.microsoft.com/entra/identity-platform/v2-oauth2-device-code
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config, token_path, write_json_private
from .errors import AuthError
from .http import RetryPolicy, form_encode, request

log = logging.getLogger("mstodo.auth")

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

#: Refresh this many seconds before the access token actually expires, so a slow
#: request started just under the wire does not land on an expired token.
EXPIRY_MARGIN_SECONDS = 120

#: Errors that mean "keep polling"; everything else terminates the login.
_PENDING = {"authorization_pending"}
_SLOW_DOWN = {"slow_down"}
_FATAL_HINTS = {
    "authorization_declined": "you declined the sign-in prompt",
    "expired_token": "the device code expired before sign-in completed; run auth login again",
    "bad_verification_code": "the device code was not recognised by the identity platform",
    "invalid_client": (
        "the client id is unknown, or the app is not registered as a public client. "
        "Enable 'Allow public client flows' on the app registration."
    ),
    "invalid_grant": "the identity platform rejected the grant; sign in again",
    "invalid_scope": "the app registration does not expose one of the requested scopes",
}

TOKEN_CACHE_VERSION = 1


@dataclass
class DeviceCodePrompt:
    """What the user must be shown to complete sign-in."""

    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    message: str

    def render(self) -> str:
        return (
            f"\n  To sign in, open {self.verification_uri}\n"
            f"  and enter code:  {self.user_code}\n\n"
            f"  The code expires in {self.expires_in // 60} minutes.\n"
        )


@dataclass
class Account:
    username: str | None = None
    name: str | None = None
    tenant_id: str | None = None
    object_id: str | None = None

    def label(self) -> str:
        return self.username or self.name or "unknown account"

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "name": self.name,
            "tenant_id": self.tenant_id,
            "object_id": self.object_id,
        }


def _decode_id_token_claims(id_token: str | None) -> Account:
    """Read display claims out of *our own* id_token.

    Deliberately no signature validation: this value is used for nothing but
    printing who is signed in. Access tokens are never parsed — Microsoft's docs
    are explicit that tokens for APIs you do not own may not even be JWTs.
    """
    account = Account()
    if not id_token:
        return account
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return account
    account.username = claims.get("preferred_username") or claims.get("email") or claims.get("upn")
    account.name = claims.get("name")
    account.tenant_id = claims.get("tid")
    account.object_id = claims.get("oid")
    return account


class TokenCache:
    """The token file. Always 0600, always written atomically."""

    def __init__(self, path: Any = None) -> None:
        self.path = path or token_path()

    def load(self) -> dict[str, Any] | None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("token cache at %s is unreadable; treating as signed out", self.path)
            return None
        if data.get("version") != TOKEN_CACHE_VERSION:
            log.warning("token cache version mismatch; sign in again")
            return None
        return data

    def save(self, data: dict[str, Any]) -> None:
        write_json_private(self.path, data)

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:  # pragma: no cover - permission oddities
            raise AuthError(f"could not remove {self.path}: {exc}") from exc


class DeviceCodeAuth:
    """Acquires and refreshes access tokens, caching them on disk."""

    def __init__(
        self,
        config: Config,
        *,
        cache: TokenCache | None = None,
        policy: RetryPolicy | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.cache = cache or TokenCache()
        self.policy = policy or RetryPolicy(attempts=config.retries)
        self.clock = clock
        self.sleep = sleep
        self.opener = opener
        self._state: dict[str, Any] | None = None

    # -- endpoints ---------------------------------------------------------

    def _endpoint(self, leaf: str) -> str:
        return f"{self.config.authority}/{self.config.tenant}/oauth2/v2.0/{leaf}"

    def _post_form(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = request(
            "POST",
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=form_encode(payload),
            timeout=self.config.timeout,
            policy=self.policy,
            opener=self.opener,
        )
        data = resp.json()
        if data is None:
            raise AuthError(
                f"identity platform returned HTTP {resp.status} with a non-JSON body",
                hint=resp.text[:400] or None,
            )
        return data

    # -- login -------------------------------------------------------------

    def begin_device_flow(self) -> tuple[DeviceCodePrompt, str]:
        scope = " ".join(self.config.scopes)
        data = self._post_form(
            self._endpoint("devicecode"),
            {"client_id": self.config.client_id, "scope": scope},
        )
        if "device_code" not in data:
            raise AuthError(
                data.get("error_description") or data.get("error") or "device code request failed",
                hint=_FATAL_HINTS.get(str(data.get("error"))),
            )
        prompt = DeviceCodePrompt(
            user_code=data["user_code"],
            verification_uri=data["verification_uri"],
            expires_in=int(data.get("expires_in", 900)),
            interval=int(data.get("interval", 5)),
            message=data.get("message", ""),
        )
        return prompt, data["device_code"]

    def poll_for_token(self, prompt: DeviceCodePrompt, device_code: str) -> Account:
        """Poll the token endpoint until the user approves, declines or times out."""
        interval = max(1, prompt.interval)
        deadline = self.clock() + prompt.expires_in

        while True:
            if self.clock() >= deadline:
                raise AuthError(
                    "timed out waiting for sign-in approval",
                    hint="run auth login again and complete the browser step within 15 minutes",
                )
            self.sleep(interval)
            data = self._post_form(
                self._endpoint("token"),
                {
                    "grant_type": DEVICE_CODE_GRANT,
                    "client_id": self.config.client_id,
                    "device_code": device_code,
                },
            )
            if "access_token" in data:
                return self._store(data)

            error = str(data.get("error", "unknown_error"))
            if error in _PENDING:
                continue
            if error in _SLOW_DOWN:
                interval += 5
                continue
            raise AuthError(
                data.get("error_description", error).split("\r\n")[0],
                hint=_FATAL_HINTS.get(error),
            )

    def login(self, on_prompt: Callable[[DeviceCodePrompt], None]) -> Account:
        prompt, device_code = self.begin_device_flow()
        on_prompt(prompt)
        return self.poll_for_token(prompt, device_code)

    # -- token lifecycle ---------------------------------------------------

    def _store(self, token_response: dict[str, Any]) -> Account:
        existing = self._state or self.cache.load() or {}
        account = _decode_id_token_claims(token_response.get("id_token"))
        if not account.username and existing.get("account"):
            account = Account(**existing["account"])

        state = {
            "version": TOKEN_CACHE_VERSION,
            "client_id": self.config.client_id,
            "tenant": self.config.tenant,
            "authority": self.config.authority,
            "scope": token_response.get("scope", " ".join(self.config.scopes)),
            "access_token": token_response["access_token"],
            # Refresh tokens rotate: always keep the newest one the server sent.
            "refresh_token": token_response.get("refresh_token") or existing.get("refresh_token"),
            "expires_at": self.clock() + float(token_response.get("expires_in", 3600)),
            "obtained_at": self.clock(),
            "account": account.to_dict(),
        }
        self.cache.save(state)
        self._state = state
        return account

    def _load_state(self) -> dict[str, Any]:
        if self._state is None:
            self._state = self.cache.load()
        if not self._state:
            raise AuthError("not signed in", hint="run: todo.py auth login")

        configured = self.config.client_id_or_none
        if configured and self._state.get("client_id") != configured:
            raise AuthError(
                "cached token belongs to a different client id",
                hint="run: todo.py auth logout && todo.py auth login",
            )
        if self._state.get("tenant") != self.config.tenant:
            raise AuthError(
                f"cached token is for tenant '{self._state.get('tenant')}', "
                f"but '{self.config.tenant}' is configured",
                hint="run: todo.py auth logout && todo.py auth login",
            )
        return self._state

    def refresh(self) -> Account:
        state = self._load_state()
        refresh_token = state.get("refresh_token")
        if not refresh_token:
            raise AuthError(
                "no refresh token cached",
                hint="the 'offline_access' scope is required; run: todo.py auth login",
            )
        data = self._post_form(
            self._endpoint("token"),
            {
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "refresh_token": refresh_token,
                "scope": " ".join(self.config.scopes),
            },
        )
        if "access_token" not in data:
            error = str(data.get("error", "unknown_error"))
            raise AuthError(
                data.get("error_description", error).split("\r\n")[0],
                hint=_FATAL_HINTS.get(error, "run: todo.py auth login"),
            )
        return self._store(data)

    def access_token(self, *, force_refresh: bool = False) -> str:
        state = self._load_state()
        expired = self.clock() >= float(state.get("expires_at", 0)) - EXPIRY_MARGIN_SECONDS
        if force_refresh or expired:
            self.refresh()
            state = self._state or {}
        return str(state["access_token"])

    # -- introspection -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        state = self.cache.load()
        if not state:
            return {"signed_in": False}
        expires_at = float(state.get("expires_at", 0))
        return {
            "signed_in": True,
            "account": state.get("account", {}),
            "client_id": state.get("client_id"),
            "tenant": state.get("tenant"),
            "scope": state.get("scope"),
            "access_token_expires_in": max(0, int(expires_at - self.clock())),
            "has_refresh_token": bool(state.get("refresh_token")),
            "token_file": str(self.cache.path),
        }

    def logout(self) -> bool:
        self._state = None
        return self.cache.clear()
