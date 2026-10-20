"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


def _endpoint_root(url: str) -> str:
    """'https://x.openai.azure.com/openai/deployments/...?...' -> 'https://x.openai.azure.com' (the SDK adds the path)."""
    url = url.strip()
    if not url:
        return ""
    parts = urlsplit(url if "://" in url else f"https://{url}")
    return f"{parts.scheme}://{parts.netloc}"


def _csv(value: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in value.split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    adx_cluster_uri: str
    adx_database: str
    auth_mode: str = "dev"  # "obo" | "dev"
    anthropic_model: str = "claude-sonnet-5-5"
    llm_provider: str = "anthropic"  # "anthropic" | "foundry" | "openai" | "azure_openai"
    openai_model: str = "gpt-5"
    azure_openai_endpoint: str = ""       # https://<resource>.openai.azure.com (or .cognitiveservices / .services.ai)
    azure_openai_deployment: str = ""     # the deployment name in Foundry; used as the model
    azure_openai_auth: str = "entra"      # "entra" (managed identity / az login) | "key"
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2025-04-01-preview"
    openai_reasoning_effort: str = "medium"  # "" = provider default
    anthropic_effort: str = "medium"  # Claude output_config.effort; "" = model default
    foundry_resource: str = ""
    foundry_auth: str = "entra"  # "entra" (managed identity / az login) | "key"
    foundry_api_key: str = ""
    allowed_entities: tuple[str, ...] = ("Costs_v1_2()", "Recommendations()")
    schema_file: str | None = None
    dashboard_file: str | None = None  # ADX dashboard export whose tiles become run_dashboard_query
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: str = ""
    entra_credential: str = "secret"  # "secret" | "managed_identity" (federated credential, no secret)
    entra_mi_client_id: str = ""      # client id of the user-assigned managed identity the app registration trusts
    max_rows: int = 500          # server-side row cap per query
    llm_rows: int = 30           # rows shown to the model (the UI preview shows its own)
    max_result_chars: int = 8_000
    query_timeout_s: int = 60
    max_tool_iterations: int = 8   # rounds; the weekly report needs ~5 when some queries need a fix
    max_turn_tokens: int = 150_000  # input + output tokens one question may use before the agent stops
    admin_users: tuple[str, ...] = ()       # lowercase sign-in names allowed on /admin
    admin_group_ids: tuple[str, ...] = ()   # Entra group object ids allowed on /admin
    admin_log_workspace_id: str = ""        # Log Analytics workspace (customer) id the admin page reads
    container_app_name: str = ""            # set by Container Apps; filters the workspace's logs
    price_input: float = 0.0                # USD per million tokens, for the admin page's cost estimate
    price_cached: float = 0.0
    price_output: float = 0.0
    max_history_turns: int = 8
    a2a_enabled: bool = False       # A2A endpoint (a2a_server.py): /.well-known/agent-card.json and /a2a
    public_url: str = ""            # the app's https URL (CHAINLIT_URL); the agent card advertises it
    max_query_chars: int = 8_000
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        e = os.environ if env is None else env
        auth_mode = e.get("AUTH_MODE", "dev").lower()
        if auth_mode not in ("obo", "dev"):
            raise ValueError("AUTH_MODE must be 'obo' or 'dev'")
        llm_provider = e.get("LLM_PROVIDER", "anthropic").lower()
        if llm_provider not in ("anthropic", "foundry", "openai", "azure_openai"):
            raise ValueError("LLM_PROVIDER must be 'anthropic', 'foundry', 'openai' or 'azure_openai'")
        azure_openai_auth = e.get("AZURE_OPENAI_AUTH", "entra").lower()
        if azure_openai_auth not in ("entra", "key"):
            raise ValueError("AZURE_OPENAI_AUTH must be 'entra' or 'key'")
        foundry_auth = e.get("FOUNDRY_AUTH", "entra").lower()
        if foundry_auth not in ("entra", "key"):
            raise ValueError("FOUNDRY_AUTH must be 'entra' or 'key'")
        entra_credential = e.get("ENTRA_CREDENTIAL", "secret").lower()
        if entra_credential not in ("secret", "managed_identity"):
            raise ValueError("ENTRA_CREDENTIAL must be 'secret' or 'managed_identity'")
        s = cls(
            adx_cluster_uri=e.get("ADX_CLUSTER_URI", "").rstrip("/"),
            adx_database=e.get("ADX_DATABASE", "Hub"),
            auth_mode=auth_mode,
            anthropic_model=e.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            llm_provider=llm_provider,
            openai_model=e.get("OPENAI_MODEL", "gpt-5"),
            azure_openai_endpoint=_endpoint_root(e.get("AZURE_OPENAI_ENDPOINT", "")),
            azure_openai_deployment=e.get("AZURE_OPENAI_DEPLOYMENT", ""),
            azure_openai_auth=azure_openai_auth,
            azure_openai_api_key=e.get("AZURE_OPENAI_API_KEY", ""),
            azure_openai_api_version=e.get("AZURE_OPENAI_API_VERSION", "") or "2025-04-01-preview",
            openai_reasoning_effort=e.get("OPENAI_REASONING_EFFORT", "medium").strip().lower(),
            anthropic_effort=e.get("ANTHROPIC_EFFORT", "medium").strip().lower(),
            foundry_resource=e.get("ANTHROPIC_FOUNDRY_RESOURCE", ""),
            foundry_auth=foundry_auth,
            foundry_api_key=e.get("ANTHROPIC_FOUNDRY_API_KEY", ""),
            allowed_entities=_csv(e.get("ALLOWED_ENTITIES", "Costs_v1_2(),Recommendations()")),
            schema_file=e.get("SCHEMA_FILE") or None,
            dashboard_file=e.get("DASHBOARD_FILE") or None,
            entra_tenant_id=e.get("ENTRA_TENANT_ID", ""),
            entra_client_id=e.get("ENTRA_CLIENT_ID", ""),
            entra_client_secret=e.get("ENTRA_CLIENT_SECRET", ""),
            entra_credential=entra_credential,
            entra_mi_client_id=e.get("ENTRA_MI_CLIENT_ID", ""),
            max_rows=int(e.get("MAX_ROWS", 500)),
            llm_rows=int(e.get("LLM_ROWS", 30)),
            query_timeout_s=int(e.get("QUERY_TIMEOUT_SECONDS", 60)),
            max_tool_iterations=int(e.get("MAX_TOOL_ITERATIONS", 8)),
            max_turn_tokens=int(e.get("MAX_TURN_TOKENS", 150_000)),
            admin_users=tuple(u.lower() for u in _csv(e.get("ADMIN_USERS", ""))),
            admin_group_ids=tuple(g.lower() for g in _csv(e.get("ADMIN_GROUP_IDS", ""))),
            admin_log_workspace_id=e.get("ADMIN_LOG_WORKSPACE_ID", ""),
            container_app_name=e.get("CONTAINER_APP_NAME", ""),
            price_input=float(e.get("PRICE_INPUT_PER_MTOK", 0) or 0),
            price_cached=float(e.get("PRICE_CACHED_PER_MTOK", 0) or 0),
            price_output=float(e.get("PRICE_OUTPUT_PER_MTOK", 0) or 0),
            a2a_enabled=e.get("A2A_ENABLED", "").strip().lower() in ("1", "true", "yes"),
            public_url=e.get("CHAINLIT_URL", "").strip().rstrip("/"),
        )
        return s

    @property
    def model(self) -> str:
        if self.llm_provider == "azure_openai":
            return self.azure_openai_deployment
        return self.openai_model if self.llm_provider == "openai" else self.anthropic_model

    def validate(self) -> None:
        if not self.adx_cluster_uri:
            raise ValueError("ADX_CLUSTER_URI is required")
        if self.auth_mode == "obo":
            if not (self.entra_tenant_id and self.entra_client_id):
                raise ValueError("AUTH_MODE=obo requires ENTRA_TENANT_ID and ENTRA_CLIENT_ID")
            if self.entra_credential == "secret" and not self.entra_client_secret:
                raise ValueError("ENTRA_CREDENTIAL=secret requires ENTRA_CLIENT_SECRET")
            if self.entra_credential == "managed_identity" and not self.entra_mi_client_id:
                raise ValueError("ENTRA_CREDENTIAL=managed_identity requires ENTRA_MI_CLIENT_ID")
        if self.a2a_enabled and self.auth_mode == "obo" and not self.public_url.startswith("https://"):
            raise ValueError("A2A_ENABLED requires CHAINLIT_URL (the app's https URL) for the agent card")
        if self.llm_provider == "azure_openai" and not (self.azure_openai_endpoint and self.azure_openai_deployment):
            raise ValueError("LLM_PROVIDER=azure_openai requires AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT")
        if self.llm_provider == "azure_openai" and self.azure_openai_auth == "key" and not self.azure_openai_api_key:
            raise ValueError("AZURE_OPENAI_AUTH=key requires AZURE_OPENAI_API_KEY")
        if self.llm_provider == "foundry" and not self.foundry_resource:
            raise ValueError("LLM_PROVIDER=foundry requires ANTHROPIC_FOUNDRY_RESOURCE")
        if self.llm_provider == "foundry" and self.foundry_auth == "key" and not self.foundry_api_key:
            raise ValueError("FOUNDRY_AUTH=key requires ANTHROPIC_FOUNDRY_API_KEY")
