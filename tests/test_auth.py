import base64
import json
import os
import stat

import pytest

from mstodo.auth import DeviceCodeAuth, TokenCache, _decode_id_token_claims
from mstodo.config import Config, token_path
from mstodo.errors import AuthError
from mstodo.http import RetryPolicy


def make_id_token(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


def make_auth(transport, monkeypatch, **overrides):
    monkeypatch.setenv("MSTODO_CLIENT_ID", overrides.pop("client_id", "test-client"))
    config = Config(**overrides)
    clock = {"t": 1_000_000.0}
    return DeviceCodeAuth(
        config,
        policy=RetryPolicy(attempts=2, sleep=lambda s: None, rand=lambda: 0.0),
        clock=lambda: clock["t"],
        sleep=lambda s: None,
        opener=transport.opener,
    ), clock


DEVICE_RESPONSE = {
    "device_code": "DEV-CODE",
    "user_code": "ABCD-1234",
    "verification_uri": "https://microsoft.com/devicelogin",
    "expires_in": 900,
    "interval": 5,
    "message": "go here",
}

TOKEN_RESPONSE = {
    "token_type": "Bearer",
    "scope": "Tasks.ReadWrite",
    "expires_in": 3600,
    "access_token": "ACCESS-1",
    "refresh_token": "REFRESH-1",
    "id_token": make_id_token({"preferred_username": "user@example.com", "name": "A User", "tid": "t1"}),
}


def test_login_polls_until_approval(transport, monkeypatch):
    auth, _ = make_auth(transport, monkeypatch)
    transport.json("POST", "/devicecode", DEVICE_RESPONSE)
    transport.add("POST", "/token", status=400, json_body={"error": "authorization_pending"})
    transport.add("POST", "/token", status=400, json_body={"error": "authorization_pending"})
    transport.json("POST", "/token", TOKEN_RESPONSE)

    prompts = []
    account = auth.login(prompts.append)

    assert account.username == "user@example.com"
    assert prompts[0].user_code == "ABCD-1234"
    assert len(transport.requests_to("/token")) == 3


def test_slow_down_increases_the_polling_interval(transport, monkeypatch):
    auth, _ = make_auth(transport, monkeypatch)
    transport.json("POST", "/devicecode", DEVICE_RESPONSE)
    transport.add("POST", "/token", status=400, json_body={"error": "slow_down"})
    transport.json("POST", "/token", TOKEN_RESPONSE)

    slept: list[float] = []
    auth.sleep = slept.append
    auth.login(lambda p: None)

    assert slept == [5, 10]  # interval bumped by five seconds


@pytest.mark.parametrize(
    "error,fragment",
    [
        ("authorization_declined", "declined"),
        ("expired_token", "expired"),
        ("bad_verification_code", "not recognised"),
    ],
)
def test_fatal_polling_errors_stop_immediately(transport, monkeypatch, error, fragment):
    auth, _ = make_auth(transport, monkeypatch)
    transport.json("POST", "/devicecode", DEVICE_RESPONSE)
    transport.add("POST", "/token", status=400, json_body={"error": error, "error_description": error})

    with pytest.raises(AuthError) as excinfo:
        auth.login(lambda p: None)

    assert fragment in (excinfo.value.hint or "")
    assert len(transport.requests_to("/token")) == 1


def test_login_times_out_when_the_code_expires(transport, monkeypatch):
    auth, clock = make_auth(transport, monkeypatch)
    transport.json("POST", "/devicecode", {**DEVICE_RESPONSE, "expires_in": 10})
    transport.add("POST", "/token", status=400, json_body={"error": "authorization_pending"}, repeat=True)
    auth.sleep = lambda seconds: clock.__setitem__("t", clock["t"] + 60)

    with pytest.raises(AuthError, match="timed out"):
        auth.login(lambda p: None)


@pytest.mark.skipif(
    os.name == "nt",
    reason="NTFS has no POSIX mode bits; os.chmod only toggles read-only there. "
    "Windows confidentiality comes from the user-profile ACL — see SECURITY.md.",
)
def test_token_file_is_written_0600(transport, monkeypatch):
    auth, _ = make_auth(transport, monkeypatch)
    transport.json("POST", "/devicecode", DEVICE_RESPONSE)
    transport.json("POST", "/token", TOKEN_RESPONSE)

    auth.login(lambda p: None)

    mode = stat.S_IMODE(os.stat(token_path()).st_mode)
    assert mode == 0o600, f"token cache must not be readable by others, got {oct(mode)}"


def test_token_file_is_written_inside_the_config_dir(transport, monkeypatch, isolated_config):
    """Platform-independent half of the guarantee: the token never escapes the
    user-scoped config directory, which is what protects it on Windows."""
    auth, _ = make_auth(transport, monkeypatch)
    transport.json("POST", "/devicecode", DEVICE_RESPONSE)
    transport.json("POST", "/token", TOKEN_RESPONSE)

    auth.login(lambda p: None)

    written = token_path()
    assert written.is_file()
    assert written.parent == isolated_config


def test_access_token_refreshes_when_close_to_expiry(transport, monkeypatch):
    auth, _clock = make_auth(transport, monkeypatch)
    transport.json("POST", "/devicecode", DEVICE_RESPONSE)
    transport.json("POST", "/token", {**TOKEN_RESPONSE, "expires_in": 60})
    auth.login(lambda p: None)

    transport.json("POST", "/token", {**TOKEN_RESPONSE, "access_token": "ACCESS-2", "refresh_token": "REFRESH-2"})
    token = auth.access_token()

    assert token == "ACCESS-2"
    refresh_call = transport.requests_to("/token")[-1]
    assert b"grant_type=refresh_token" in refresh_call["body"]


def test_valid_token_is_reused_without_a_network_call(transport, monkeypatch):
    auth, _ = make_auth(transport, monkeypatch)
    transport.json("POST", "/devicecode", DEVICE_RESPONSE)
    transport.json("POST", "/token", TOKEN_RESPONSE)
    auth.login(lambda p: None)
    before = len(transport.calls)

    assert auth.access_token() == "ACCESS-1"
    assert len(transport.calls) == before


def test_rotated_refresh_token_replaces_the_old_one(transport, monkeypatch):
    auth, _ = make_auth(transport, monkeypatch)
    transport.json("POST", "/devicecode", DEVICE_RESPONSE)
    transport.json("POST", "/token", TOKEN_RESPONSE)
    auth.login(lambda p: None)

    transport.json("POST", "/token", {**TOKEN_RESPONSE, "refresh_token": "REFRESH-ROTATED"})
    auth.refresh()

    assert TokenCache().load()["refresh_token"] == "REFRESH-ROTATED"


def test_refresh_without_a_cached_token_is_an_auth_error(transport, monkeypatch):
    auth, _ = make_auth(transport, monkeypatch)
    with pytest.raises(AuthError, match="not signed in"):
        auth.access_token()


def test_tenant_change_invalidates_the_cached_token(transport, monkeypatch, signed_in):
    signed_in(tenant="contoso.onmicrosoft.com")
    monkeypatch.setenv("MSTODO_TENANT", "common")
    auth, _ = make_auth(transport, monkeypatch)

    with pytest.raises(AuthError, match="cached token is for tenant"):
        auth.access_token()


def test_client_id_change_invalidates_the_cached_token(transport, monkeypatch, signed_in):
    signed_in(client_id="old-client")
    auth, _ = make_auth(transport, monkeypatch, client_id="new-client")

    with pytest.raises(AuthError, match="different client id"):
        auth.access_token()


def test_status_and_logout(transport, monkeypatch, signed_in):
    signed_in()
    auth, _ = make_auth(transport, monkeypatch)

    status = auth.status()
    assert status["signed_in"] is True
    assert status["account"]["username"] == "tester@example.com"
    assert status["has_refresh_token"] is True

    assert auth.logout() is True
    assert auth.status() == {"signed_in": False}
    assert auth.logout() is False


def test_corrupt_token_cache_reads_as_signed_out(transport, monkeypatch):
    token_path().parent.mkdir(parents=True, exist_ok=True)
    token_path().write_text("{ not json")
    assert TokenCache().load() is None


def test_id_token_decoding_is_defensive():
    assert _decode_id_token_claims(None).username is None
    assert _decode_id_token_claims("not-a-jwt").username is None
    assert _decode_id_token_claims("a.!!!!.c").username is None
    assert _decode_id_token_claims(make_id_token({"email": "e@x.com"})).username == "e@x.com"
