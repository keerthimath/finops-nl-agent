"""A2A (Agent2Agent) pieces that don't need the a2a SDK: caller authentication, per-user conversations, and the
structured result an answer carries. The SDK glue is a2a_server.py.

Security model (same as the chat): the calling agent sends the user's DELEGATED Entra token for this app's API
(`api://<client-id>/access_as_user`). It is validated here, exchanged on-behalf-of for an ADX token, and ADX RLS
decides what the user sees. App-only (client-credentials) tokens are refused: there is no shared-credential path.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .tools import QUERY_TOOLS, SessionProfile

SCOPE = "access_as_user"
APP_ONLY_REFUSED = ("App-only tokens are not accepted. Send a token for the signed-in user "
                    f"(delegated scope {SCOPE}), e.g. from an on-behalf-of exchange.")


class A2AAuthError(Exception):
    """The caller is not authenticated. The message is safe to return (it never contains the token)."""


@dataclass(frozen=True)
class Principal:
    identity: str  # lowercase sign-in name, the same identifier the chat uses
    token: str     # the user's access token for this app; the OBO assertion. Never log it.


def check_claims(claims: dict, tenant_id: str) -> str:
    """The user's identity from an already signature-checked token, or A2AAuthError."""
    if str(claims.get("tid", "")).lower() != tenant_id.lower():
        raise A2AAuthError("Token is from another tenant.")
    scopes = str(claims.get("scp", "")).split()
    if not scopes:
        raise A2AAuthError(APP_ONLY_REFUSED)
    if SCOPE not in scopes:
        raise A2AAuthError(f"Token lacks the {SCOPE} scope.")
    # v1 tokens of guest / Microsoft-account users carry only unique_name ("live.com#user@x.com"): prefer email.
    identity = (claims.get("preferred_username") or claims.get("upn") or claims.get("email")
                or claims.get("unique_name") or "").lower()
    if not identity:
        raise A2AAuthError("Token has no user name claim.")
    return identity


class TokenValidator:
    """Validates a bearer token for this app's API: Entra signature, lifetime, issuer, audience, then check_claims.

    Accepts v1 and v2 access tokens (which one Entra issues depends on the app manifest's
    requestedAccessTokenVersion). validate() may fetch Entra's signing keys: call it off the event loop.
    """

    def __init__(self, tenant_id: str, client_id: str, jwks_client: Any = None, leeway_s: int = 60):
        self._tenant = tenant_id
        self._audiences = [f"api://{client_id}", client_id]
        self._issuers = [f"https://login.microsoftonline.com/{tenant_id}/v2.0", f"https://sts.windows.net/{tenant_id}/"]
        self._jwks = jwks_client
        self._leeway = leeway_s

    def _keys(self):
        if self._jwks is None:
            import jwt

            self._jwks = jwt.PyJWKClient(f"https://login.microsoftonline.com/{self._tenant}/discovery/v2.0/keys",
                                         cache_keys=True, lifespan=3600)
        return self._jwks

    def validate(self, token: str) -> Principal:
        import jwt

        try:
            key = self._keys().get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self._audiences, issuer=self._issuers,
                                leeway=self._leeway, options={"require": ["exp", "iat", "aud", "iss"]})
        except jwt.PyJWTError as exc:
            raise A2AAuthError(f"Invalid token: {exc}") from exc
        return Principal(check_claims(claims, self._tenant), token)


def bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise A2AAuthError("Send the user's Entra access token as 'Authorization: Bearer <token>'.")
    return token.strip()


@dataclass
class Conversation:
    """One A2A context (contextId) of one user: what the chat keeps in its user session."""
    history: list = field(default_factory=list)
    profile: SessionProfile | None = None
    unavailable_tiles: set = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one turn at a time per conversation
    used: float = 0.0


class Conversations:
    """In memory, keyed by (identity, contextId), so a contextId never reaches another user's history.

    Like TokenStore: one replica only. Idle conversations expire; past max_items the least recently used goes.
    """

    def __init__(self, max_items: int = 500, ttl_s: float = 4 * 3600, clock: Callable[[], float] = time.monotonic):
        self._items: dict[tuple[str, str], Conversation] = {}
        self._max, self._ttl, self._clock = max_items, ttl_s, clock

    def get(self, identity: str, context_id: str) -> Conversation:
        now = self._clock()
        for key in [k for k, c in self._items.items() if now - c.used > self._ttl]:
            del self._items[key]
        key = (identity.lower(), context_id)
        conv = self._items.pop(key, None) or Conversation()
        conv.used = now
        self._items[key] = conv  # dicts keep insertion order: most recently used last
        while len(self._items) > self._max:
            del self._items[next(iter(self._items))]
        return conv

    def __len__(self) -> int:
        return len(self._items)


def query_results(turn, max_rows: int = 100) -> dict:
    """The rows behind an answer, for agents that compute with them: one entry per query that ran and succeeded."""
    out = []
    for o in turn.outcomes:
        if o.name not in QUERY_TOOLS or o.repeat or o.is_error or o.result is None:
            continue
        r = o.result
        out.append({
            "source": o.source or "KQL (written by the assistant)",
            "kql": o.kql or "",
            "columns": list(r.columns),
            "rows": r.rows[:max_rows],
            "row_count": r.row_count,
            "truncated": r.truncated or r.row_count > max_rows,
        })
    return {"queries": out}
