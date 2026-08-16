"""Configuration and on-disk state locations.

Resolution order for every setting: explicit argument > environment variable >
config file > built-in default. Nothing here reaches the network.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from .errors import ConfigError

APP_DIR_NAME = "ms-todo"

DEFAULT_AUTHORITY = "https://login.microsoftonline.com"
DEFAULT_TENANT = "common"
DEFAULT_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

#: Least-privileged scope set. ``Tasks.ReadWrite`` covers every write path in the
#: To Do API; ``offline_access`` is what gets us a refresh token.
DEFAULT_SCOPES = ("offline_access", "openid", "profile", "Tasks.ReadWrite")

#: Microsoft's own multi-tenant public client, shipped with the Graph CLI and the
#: Graph PowerShell SDK. Handy for a first run; register your own app for anything
#: real so consent and audit logs point at *you*. See references/setup.md.
GRAPH_CLI_PUBLIC_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"

_NO_CLIENT_ID_HINT = (
    "Set MSTODO_CLIENT_ID, or run: todo.py auth login --client-id <id> --save.\n"
    "      Register a public-client app (allowPublicClientFlows=true) with the\n"
    "      delegated Tasks.ReadWrite scope — see references/setup.md.\n"
    f"      To evaluate quickly you may use Microsoft's Graph CLI client id\n"
    f"      {GRAPH_CLI_PUBLIC_CLIENT_ID}, but prefer your own for real use."
)


def config_dir() -> Path:
    """Directory holding config, token cache and sync state."""
    override = os.environ.get("MSTODO_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / APP_DIR_NAME


def ensure_config_dir() -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, stat.S_IRWXU)  # 0700
    except OSError:  # pragma: no cover - platform dependent
        pass
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


def token_path() -> Path:
    return config_dir() / "token.json"


def list_cache_path() -> Path:
    return config_dir() / "lists-cache.json"


def delta_state_path() -> Path:
    return config_dir() / "delta-state.json"


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ConfigError(
            f"could not read {path}: {exc}",
            hint="delete the file to start clean, or fix the JSON by hand",
        ) from exc


def write_json_private(path: Path, payload: Any) -> None:
    """Write JSON atomically with 0600 permissions.

    The token cache lives here, so the file must never be world-readable — not
    even transiently, which is why the mode is applied to the temp file before
    the rename rather than after.
    """
    ensure_config_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:  # pragma: no cover - platform dependent
        pass


class Config:
    """Effective settings for one CLI invocation."""

    def __init__(self, **overrides: Any) -> None:
        self._file = read_json(config_path(), default={}) or {}
        self._over = {k: v for k, v in overrides.items() if v is not None}

    def _get(self, key: str, env: str, default: Any) -> Any:
        if key in self._over:
            return self._over[key]
        if os.environ.get(env):
            return os.environ[env]
        if key in self._file and self._file[key] is not None:
            return self._file[key]
        return default

    @property
    def client_id(self) -> str:
        value = self._get("client_id", "MSTODO_CLIENT_ID", None)
        if not value:
            raise ConfigError("no Microsoft Entra client id configured", hint=_NO_CLIENT_ID_HINT)
        return str(value)

    @property
    def client_id_or_none(self) -> str | None:
        return self._get("client_id", "MSTODO_CLIENT_ID", None)

    @property
    def tenant(self) -> str:
        return str(self._get("tenant", "MSTODO_TENANT", DEFAULT_TENANT))

    @property
    def authority(self) -> str:
        return str(self._get("authority", "MSTODO_AUTHORITY", DEFAULT_AUTHORITY)).rstrip("/")

    @property
    def graph_base(self) -> str:
        return str(self._get("graph_base", "MSTODO_GRAPH_BASE", DEFAULT_GRAPH_BASE)).rstrip("/")

    @property
    def scopes(self) -> list[str]:
        raw = self._get("scopes", "MSTODO_SCOPES", None)
        if not raw:
            return list(DEFAULT_SCOPES)
        if isinstance(raw, str):
            return raw.split()
        return list(raw)

    @property
    def timezone(self) -> str | None:
        return self._get("timezone", "MSTODO_TIMEZONE", None)

    @property
    def timeout(self) -> float:
        return float(self._get("timeout", "MSTODO_TIMEOUT", 30))

    @property
    def retries(self) -> int:
        return int(self._get("retries", "MSTODO_RETRIES", 5))

    def save(self, **values: Any) -> Path:
        """Persist selected settings to config.json, merging with what's there."""
        merged = dict(self._file)
        for key, value in values.items():
            if value is not None:
                merged[key] = value
        write_json_private(config_path(), merged)
        self._file = merged
        return config_path()
