"""Entra client authentication for this app, and the on-behalf-of exchange (user's API token -> ADX token)."""
from __future__ import annotations

from typing import Callable

from .config import Settings
from .session_tokens import AuthExpired

# Audience Entra expects on a managed-identity token used as a federated client assertion.
FIC_SCOPE = "api://AzureADTokenExchange/.default"
JWT_BEARER = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


def managed_identity_assertion(mi_client_id: str) -> Callable[[], str]:
    """Returns a callable producing a managed-identity token; azure-identity caches it until near expiry."""
    from azure.identity import ManagedIdentityCredential

    cred = ManagedIdentityCredential(client_id=mi_client_id)
    return lambda: cred.get_token(FIC_SCOPE).token


class ClientAuth:
    """How the app registration proves itself to Entra: a client secret, or (ENTRA_CREDENTIAL=managed_identity)
    a federated credential where the app registration trusts the Container App's managed identity."""

    def __init__(self, settings: Settings, assertion: Callable[[], str] | None = None):
        self._secret = settings.entra_client_secret
        self._assertion = None
        if settings.entra_credential == "managed_identity":
            self._assertion = assertion or managed_identity_assertion(settings.entra_mi_client_id)

    def msal_credential(self) -> str | dict:
        if self._assertion is None:
            return self._secret
        return {"client_assertion": self._assertion}  # MSAL calls it per request

    def form(self) -> dict:
        """Fields for a raw /token request. Blocking on a managed-identity cache miss."""
        if self._assertion is None:
            return {"client_secret": self._secret}
        return {"client_assertion": self._assertion(), "client_assertion_type": JWT_BEARER}


class OboExchanger:
    def __init__(self, settings: Settings, client_auth: ClientAuth):
        import msal

        self._scope = f"{settings.adx_cluster_uri}/.default"
        self._app = msal.ConfidentialClientApplication(
            settings.entra_client_id,
            authority=f"https://login.microsoftonline.com/{settings.entra_tenant_id}",
            client_credential=client_auth.msal_credential(),
        )

    def adx_token(self, user_assertion: str) -> str:
        """Blocking; called from the ADX worker thread. MSAL caches per user."""
        result = self._app.acquire_token_on_behalf_of(user_assertion=user_assertion, scopes=[self._scope])
        token = result.get("access_token")
        if not token:
            err = result.get("error_description") or result.get("error") or "unknown error"
            if "AADSTS65001" in err or "interaction_required" in str(result.get("error")):
                raise AuthExpired("Consent for ADX access is required. Ask an admin to grant it.")
            raise AuthExpired(f"Could not get an ADX token: {err[:300]}")
        return token
