"""Multi-token auth for the MCP HTTP endpoint: identity, and per-user limits.

The server used to gate on a single shared ``MCP_AUTH_TOKEN``. That works for
any number of users (the token is not per-user and the server does not throttle
by token), but a shared secret can't be revoked for one person, can't tell you
*who* ran a query, and can't stop one heavy user from starving the others.

This adds named tokens without breaking the single-token setup:

  * ``MCP_AUTH_TOKEN``   — the original single secret; its principal is ``default``.
  * ``MCP_AUTH_TOKENS``  — ``label:token`` pairs, comma-separated, e.g.
                           ``alice:s3cr3t,bob:h4nter2``. Revoke someone by
                           dropping their pair and redeploying — everyone else's
                           token keeps working.

Both may be set at once (they merge). The matched label is the *principal*: it is
stamped into the audit log (who ran what) and is the key for per-principal rate
limiting. Identity flows to the audit sink through a ContextVar so the tool
functions need no plumbing.
"""

from __future__ import annotations

import contextvars
import hmac
from dataclasses import dataclass, field

# Set by the auth middleware once a request is identified; read by
# ``query_log.record_tool`` so each audit row records its principal. Default None
# in stdio mode (no auth) and for any unauthenticated path.
current_principal: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_principal", default=None
)


def parse_tokens(single: str | None, multi: str | None) -> dict[str, str]:
    """Build a ``{token: principal}`` map from the two env vars.

    ``single`` (``MCP_AUTH_TOKEN``) maps to principal ``default``. ``multi``
    (``MCP_AUTH_TOKENS``) is ``label:token`` pairs. Raises on a malformed pair so
    a typo fails loudly at boot rather than silently locking someone out."""
    tokens: dict[str, str] = {}
    if single:
        tokens[single] = "default"
    if multi:
        for raw in multi.split(","):
            pair = raw.strip()
            if not pair:
                continue
            if ":" not in pair:
                raise ValueError(f"MCP_AUTH_TOKENS entry {pair!r} must be 'label:token'")
            label, _, tok = pair.partition(":")
            label, tok = label.strip(), tok.strip()
            if not label or not tok:
                raise ValueError(f"MCP_AUTH_TOKENS entry {pair!r} has an empty label or token")
            tokens[tok] = label
    return tokens


def identify(provided: str, tokens: dict[str, str]) -> str | None:
    """Return the principal for ``provided``, or None if it matches no token.

    Compares against *every* token with no early return, so the time taken does
    not reveal which (or whether an early) token matched."""
    match: str | None = None
    for tok, label in tokens.items():
        if hmac.compare_digest(provided, tok):
            match = label
    return match


@dataclass
class RateLimiter:
    """A fixed-window per-key limiter. ``per_min <= 0`` disables it entirely.

    In-memory and per-process: fine for a single-container Space (one worker). It
    bounds one principal's request rate so a runaway client can't starve others;
    it is not a defense against a distributed flood (that's the host's job)."""

    per_min: int
    _window: dict[str, list[float]] = field(default_factory=dict)  # key -> [start, count]

    def allow(self, key: str, now: float) -> bool:
        if self.per_min <= 0:
            return True
        start, count = self._window.get(key, (now, 0.0))
        if now - start >= 60:
            start, count = now, 0.0
        count += 1
        self._window[key] = [start, count]
        return count <= self.per_min
