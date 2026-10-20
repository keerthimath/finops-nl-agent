"""In-memory per-user token store for the OBO flow.

Holds the user's access token (audience = this app) and refresh token server-side so they never
go into a browser cookie. In-memory means: run ONE replica (see infra/main.bicep) or swap this
class for Redis/Key Vault-backed storage before scaling out.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Awaitable, Callable


class AuthExpired(RuntimeError):
    """The user must sign in again."""


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: float


Refresher = Callable[[str], Awaitable[TokenSet]]


class TokenStore:
    def __init__(self, skew_seconds: int = 120):
        self._tokens: dict[str, TokenSet] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._skew = skew_seconds

    def put(self, user: str, tokens: TokenSet) -> None:
        self._tokens[user.lower()] = tokens

    def drop(self, user: str) -> None:
        self._tokens.pop(user.lower(), None)

    async def get_access_token(self, user: str, refresher: Refresher | None = None) -> str:
        key = user.lower()
        ts = self._tokens.get(key)
        if ts is None:
            raise AuthExpired("No session for this user.")
        if ts.expires_at - time.time() > self._skew:
            return ts.access_token
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            ts = self._tokens.get(key)
            if ts and ts.expires_at - time.time() > self._skew:
                return ts.access_token
            if not ts or not ts.refresh_token or refresher is None:
                self.drop(key)
                raise AuthExpired("Session expired.")
            try:
                new = await refresher(ts.refresh_token)
            except Exception as exc:  # noqa: BLE001
                self.drop(key)
                raise AuthExpired("Could not refresh the session.") from exc
            if not new.refresh_token:
                new.refresh_token = ts.refresh_token
            self._tokens[key] = new
            return new.access_token


def jwt_claims(token: str) -> dict:
    """Decode (NOT verify) a JWT payload. Only use on tokens received directly from the token endpoint."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return {}


token_store = TokenStore()
