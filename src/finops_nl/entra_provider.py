"""Chainlit OAuth provider for Entra ID that requests a token for THIS app's API.

Why custom: Chainlit's built-in azure-ad provider returns a Microsoft Graph token, which cannot be
exchanged on-behalf-of for an ADX token. This provider asks for `api://<client-id>/access_as_user`
plus `offline_access`, keeps the tokens server-side in `token_store`, and hands Chainlit only the
user's identity.

NOTE: written against Chainlit 2.x's OAuthProvider interface (id, env, authorize_url, token_url,
authorize_params, get_token, get_user_info). If you upgrade Chainlit, re-check that interface.
"""
from __future__ import annotations

import asyncio
import time

import httpx
from chainlit.oauth_providers import OAuthProvider, providers
from chainlit.user import User

from .auth import ClientAuth
from .config import Settings
from .session_tokens import TokenSet, jwt_claims, token_store

PROVIDER_ID = "entra-obo"


class EntraOboProvider(OAuthProvider):
    id = PROVIDER_ID
    env = ["ENTRA_TENANT_ID", "ENTRA_CLIENT_ID"]

    def __init__(self, settings: Settings, client_auth: ClientAuth) -> None:
        self.tenant = settings.entra_tenant_id
        self.client_id = settings.entra_client_id
        self.client_auth = client_auth
        base = f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0"
        self.authorize_url = f"{base}/authorize"
        self.token_url = f"{base}/token"
        self.scope = f"api://{self.client_id}/access_as_user openid profile email offline_access"
        self.authorize_params = {
            "tenant": self.tenant,
            "response_type": "code",
            "scope": self.scope,
            "response_mode": "query",
        }
        self._pending: dict[str, dict] = {}

    async def _post(self, data: dict) -> dict:
        data = {**data, **await asyncio.to_thread(self.client_auth.form)}
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(self.token_url, data=data)
        body = r.json()
        if r.status_code != 200 or "access_token" not in body:
            raise ValueError(body.get("error_description", "token request failed")[:300])
        return body

    async def get_token(self, code: str, url: str) -> str:
        body = await self._post({
            "client_id": self.client_id,
            "code": code, "grant_type": "authorization_code",
            "redirect_uri": url, "scope": self.scope,
        })
        token = body["access_token"]
        self._pending[token] = body  # picked up in get_user_info, then discarded
        return token

    async def get_user_info(self, token: str):
        body = self._pending.pop(token)
        claims = jwt_claims(body.get("id_token", ""))
        ident = (claims.get("preferred_username") or claims.get("email") or claims.get("upn") or "").lower()
        if not ident:
            raise ValueError("Entra did not return a usable user identifier.")
        token_store.put(ident, TokenSet(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_at=time.time() + int(body.get("expires_in", 3600)),
        ))
        group_ids = claims.get("groups", [])
        if not isinstance(group_ids, list):
            group_ids = []
        group_ids = [group_id.lower() for group_id in group_ids if isinstance(group_id, str)]
        user = User(identifier=ident, metadata={
            "provider": PROVIDER_ID, "name": claims.get("name", ident), "groups": group_ids,
        })
        return {"claims": {k: claims.get(k) for k in ("name", "preferred_username", "oid", "tid")}}, user

    async def refresh(self, refresh_token: str) -> TokenSet:
        body = await self._post({
            "client_id": self.client_id,
            "grant_type": "refresh_token", "refresh_token": refresh_token, "scope": self.scope,
        })
        return TokenSet(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_at=time.time() + int(body.get("expires_in", 3600)),
        )


def register(settings: Settings, client_auth: ClientAuth) -> EntraOboProvider:
    provider = EntraOboProvider(settings, client_auth)
    providers.append(provider)
    return provider
