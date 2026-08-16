"""Typed errors with stable process exit codes.

Exit codes are part of the CLI contract — scripts and CI jobs branch on them,
so they must not be renumbered without a major version bump.

    0  success
    1  generic failure
    2  usage / configuration error
    3  authentication required or failed
    4  requested resource does not exist
    5  throttled, and retries were exhausted
    6  permission denied by Graph (missing scope / consent)
"""

from __future__ import annotations

from typing import Any


class MsTodoError(Exception):
    """Base class for every error this package raises deliberately."""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def render(self) -> str:
        out = f"error: {self.message}"
        if self.hint:
            out += f"\nhint: {self.hint}"
        return out


class UsageError(MsTodoError):
    """The command was well-formed for argparse but wrong in context."""

    exit_code = 2


class ConfigError(MsTodoError):
    """Missing or invalid configuration, e.g. no client id."""

    exit_code = 2


class AuthError(MsTodoError):
    """No usable credentials, or the identity platform refused us."""

    exit_code = 3


class GraphError(MsTodoError):
    """A non-success response from Microsoft Graph."""

    exit_code = 1

    def __init__(
        self,
        message: str,
        *,
        status: int,
        code: str | None = None,
        request_id: str | None = None,
        body: Any = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.body = body

    def render(self) -> str:
        parts = [f"error: Graph {self.status}"]
        if self.code:
            parts[0] += f" {self.code}"
        parts[0] += f": {self.message}"
        if self.request_id:
            parts.append(f"request-id: {self.request_id}")
        if self.hint:
            parts.append(f"hint: {self.hint}")
        return "\n".join(parts)


class NotFoundError(GraphError):
    exit_code = 4


class ThrottledError(GraphError):
    exit_code = 5


class PermissionError_(GraphError):
    """Named with a trailing underscore to avoid shadowing the builtin."""

    exit_code = 6


class AmbiguousReferenceError(UsageError):
    """A name or id prefix matched more than one object."""

    def __init__(self, message: str, candidates: list[str]) -> None:
        preview = "\n  ".join(candidates[:10])
        extra = "" if len(candidates) <= 10 else f"\n  ... and {len(candidates) - 10} more"
        super().__init__(message, hint=f"candidates:\n  {preview}{extra}")
        self.candidates = candidates
