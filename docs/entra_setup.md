# Entra ID setup

1. **App registration** (single tenant). Note the Application (client) ID and Directory (tenant) ID.
2. **Redirect URIs** (type Web):
   - `http://localhost:8000/auth/oauth/entra-obo/callback` (local)
   - `https://<container-app-fqdn>/auth/oauth/entra-obo/callback` (Bicep output `redirectUri`)
3. **Client credential**. Deployed: no secret. The Bicep default `entraCredential=managed_identity` makes the
   app authenticate as the app registration using its managed identity. After the first deploy, run the
   `federatedCredentialCommand` output once (or in the portal: app registration -> Certificates & secrets ->
   Federated credentials -> *Managed Identity*, pick `<namePrefix>-id`). It adds a federated credential with
   issuer `https://login.microsoftonline.com/<tenant-id>/v2.0`, subject = the identity's object (principal) id,
   and audience `api://AzureADTokenExchange`.
   Testing `AUTH_MODE=obo` locally needs a client secret (`ENTRA_CREDENTIAL=secret`), because there's no managed
   identity on your machine. Keep that secret short-lived, or use a separate dev app registration.
4. **Expose an API**: set Application ID URI to `api://<client-id>`; add scope `access_as_user`
   (consent: Admins and users).
5. **API permissions**:
   - Microsoft Graph delegated: `openid`, `profile`, `email`, `offline_access`.
   - APIs my organization uses -> **Azure Data Explorer** -> delegated `user_impersonation`.
   - Your own API -> `access_as_user`.
   - **Grant admin consent** for the tenant. Without it, the on-behalf-of exchange fails with a consent error.
6. **Enterprise application** for this app: set *Assignment required* = Yes and assign the team groups,
   so only intended people can sign in.
7. **ADX access**: put each team's Entra group in `scripts/teams.yaml`; `gen_rls.py` emits the
   `viewers` grants and the RLS policy. Users need no role higher than Database Viewer.
8. Generate `CHAINLIT_AUTH_SECRET` with `chainlit create-secret`.
