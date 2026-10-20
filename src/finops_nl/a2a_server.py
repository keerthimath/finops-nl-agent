"""A2A (Agent2Agent) endpoint on the Chainlit server: GET /.well-known/agent-card.json and JSON-RPC POST /a2a.

Another agent asks a cost question for a signed-in user and gets the same answer the chat would give that user:
the bearer token (the user's delegated token for this app's API) is validated (a2a_core.TokenValidator), exchanged
on-behalf-of for an ADX token, and RLS applies. AUTH_MODE=dev uses `az login` and no token, like the chat (local only).

Imported by app.py only when A2A_ENABLED; needs `a2a-sdk[http-server]`.
"""
from __future__ import annotations

import asyncio
from importlib import metadata

from a2a.auth.user import User
from a2a.helpers.proto_helpers import new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (DefaultServerCallContextBuilder, add_a2a_routes_to_fastapi, create_agent_card_routes,
                               create_jsonrpc_routes)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from a2a.types import (AgentCapabilities, AgentCard, AgentInterface, AgentSkill,
                       AuthorizationCodeOAuthFlow, OAuth2SecurityScheme, OAuthFlows, SecurityRequirement,
                       SecurityScheme, StringList, TaskState)
from starlette.responses import JSONResponse
from starlette.routing import Route

from .a2a_core import SCOPE, A2AAuthError, Conversations, Principal, TokenValidator, bearer_token, query_results
from .adx import AdxClient
from .agent import Agent, build_system_prompt
from .config import Settings
from .logging_utils import log_event
from .session_tokens import AuthExpired
from .tools import QUERY_TOOLS, ToolRunner, session_profile

A2A_PATH = "/a2a"
SCHEME = "entra"
_PRINCIPAL = "finops_principal"  # request.scope key set by the auth gate

SKILLS = [
    ("cost-questions", "Cost questions",
     "Spend by subscription, resource group, service, resource or tag, for any period, and why it changed.",
     ["cost", "finops", "azure"],
     ["What did we spend last month, by subscription?", "Why did storage cost go up last week?"]),
    ("trends-forecasts", "Trends and forecasts",
     "Daily and monthly cost trends, run rate and month-end forecasts.",
     ["trend", "forecast"],
     ["Show the daily cost trend for the last 30 days.", "Where will we end up this month?"]),
    ("weekly-report", "Weekly anomaly report",
     "The FinOps Toolkit weekly cost report: week-over-week changes and anomalies.",
     ["report", "anomaly"],
     ["Create the weekly cost anomaly report."]),
    ("commitments", "Reservations and savings plans",
     "Commitment utilization, unused reservations, savings and reservation recommendations.",
     ["reservations", "savings plans"],
     ["How much did we waste on unused reservations last month?"]),
]


def _version() -> str:
    try:
        return metadata.version("finops-nl-agent")
    except metadata.PackageNotFoundError:
        return "0.1.0"


def build_agent_card(settings: Settings) -> AgentCard:
    base = settings.public_url or "http://localhost:8000"
    card = AgentCard(
        name="FinOps Assistant",
        description=("Answers Azure cloud cost questions from the FinOps hub (Azure Data Explorer) for the signed-in "
                     "user. Results include only the costs that user may see. Send the question as text; the answer "
                     "is Markdown plus the rows behind it (application/json)."),
        version=_version(),
        supported_interfaces=[AgentInterface(url=base + A2A_PATH, protocol_binding="JSONRPC", protocol_version="1.0")],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/markdown", "application/json"],
        skills=[AgentSkill(id=i, name=n, description=d, tags=t, examples=e) for i, n, d, t, e in SKILLS],
    )
    if settings.auth_mode == "obo":
        login = f"https://login.microsoftonline.com/{settings.entra_tenant_id}"
        scope = f"api://{settings.entra_client_id}/{SCOPE}"
        card.security_schemes[SCHEME].CopyFrom(SecurityScheme(oauth2_security_scheme=OAuth2SecurityScheme(
            description=("Microsoft Entra ID. Send the USER's access token for this scope (delegated; an agent gets "
                         "it on-behalf-of its signed-in user). App-only tokens are refused."),
            oauth2_metadata_url=f"{login}/v2.0/.well-known/openid-configuration",
            flows=OAuthFlows(authorization_code=AuthorizationCodeOAuthFlow(
                authorization_url=f"{login}/oauth2/v2.0/authorize", token_url=f"{login}/oauth2/v2.0/token",
                scopes={scope: "Ask cost questions as the signed-in user"})),
        )))
        card.security_requirements.append(SecurityRequirement(schemes={SCHEME: StringList(list=[scope])}))
    return card


class _A2AUser(User):
    def __init__(self, identity: str):
        self._identity = identity

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._identity  # the task store keys tasks by this: tasks/get never returns another user's task


class _ContextBuilder(DefaultServerCallContextBuilder):
    def build(self, request):
        ctx = super().build(request)
        principal: Principal = request.scope[_PRINCIPAL]
        ctx.user = _A2AUser(principal.identity)
        ctx.state["principal"] = principal
        ctx.state.get("headers", {}).pop("authorization", None)
        return ctx


class FinOpsExecutor(AgentExecutor):
    """One A2A message = one chat turn (Agent.ask) for the calling user, in that user's conversation."""

    def __init__(self, settings: Settings, llm, references: dict, dashboard, exchanger,
                 conversations: Conversations | None = None, adx_factory=None):
        self._s, self._llm, self._refs, self._dashboard, self._exchanger = settings, llm, references, dashboard, exchanger
        self.conversations = conversations or Conversations()
        self._adx_factory = adx_factory or self._adx

    def _adx(self, principal: Principal) -> AdxClient:
        if self._s.auth_mode == "dev":
            return AdxClient(self._s, None)
        return AdxClient(self._s, lambda: self._exchanger.adx_token(principal.token))

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        principal: Principal = context.call_context.state["principal"]
        identity = principal.identity
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        def say(text: str):
            return updater.new_agent_message([new_text_part(text)])

        question = context.get_user_input().strip()
        if not question:
            await updater.reject(say("Send a cost question as text."))
            return
        await updater.start_work()
        conv = self.conversations.get(identity, task.context_id)
        async with conv.lock:
            try:
                adx = self._adx_factory(principal)
                if conv.profile is None:  # once per conversation, as in the chat
                    conv.profile = await session_profile(adx, identity)
                runner = ToolRunner(self._s, adx, self._refs, user=identity, dashboard=self._dashboard,
                                    unavailable_tiles=conv.unavailable_tiles, profile=conv.profile)
                agent = Agent(self._s, self._llm, runner, build_system_prompt(
                    currency=conv.profile.currency or "unknown", subscriptions=conv.profile.subscriptions))

                async def on_event(kind: str, outcome) -> None:
                    if kind == "tool_result" and outcome.name in QUERY_TOOLS and not outcome.repeat:
                        label = outcome.source or "KQL (written by the assistant)"
                        await updater.update_status(TaskState.TASK_STATE_WORKING,
                                                    say(f"Ran: {label}" + (" (failed)" if outcome.is_error else "")))

                log_event("question", user=identity, text=question[:1000], channel="a2a")
                turn = await agent.ask(conv.history, question, on_event)
            except AuthExpired as exc:
                await updater.failed(say(str(exc)))
                return
            except Exception as exc:  # noqa: BLE001
                log_event("agent_error", user=identity, error=str(exc)[:500], channel="a2a")
                await updater.failed(say("Something went wrong while answering. Please try again."))
                return
        await updater.add_artifact([new_text_part(turn.answer, "text/markdown")], name="answer")
        results = query_results(turn)
        # Rows only for callers that ask for JSON: some (Foundry's A2A tool) reject any non-text part.
        accepted = context.configuration.accepted_output_modes if context.configuration else []
        if results["queries"] and "application/json" in accepted:
            await updater.add_artifact([new_data_part(results, "application/json")], name="query_results")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def _auth_gate(endpoint, settings: Settings, validator: TokenValidator | None):
    """Authenticates every JSON-RPC call before the SDK sees it; 401 without a valid user token."""

    async def guarded(request):
        if settings.auth_mode == "dev":
            principal = Principal("dev", "")
        else:
            try:
                token = bearer_token(request.headers.get("authorization"))
                principal = await asyncio.to_thread(validator.validate, token)
            except A2AAuthError as exc:
                log_event("a2a_denied", reason=str(exc)[:200])
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": str(exc)}}, status_code=401,
                    headers={"WWW-Authenticate": f'Bearer error="invalid_token", scope="api://'
                                                 f'{settings.entra_client_id}/{SCOPE}"'})
        request.scope[_PRINCIPAL] = principal
        return await endpoint(request)

    return guarded


def add_routes(server, settings: Settings, executor: FinOpsExecutor, validator: TokenValidator | None = None) -> None:
    """Adds the card and the JSON-RPC endpoint to a FastAPI app, ahead of routes already there (Chainlit's
    catch-all UI route would otherwise answer them)."""
    if settings.auth_mode == "obo" and validator is None:
        validator = TokenValidator(settings.entra_tenant_id, settings.entra_client_id)
    card = build_agent_card(settings)
    handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card)
    rpc = [Route(r.path, _auth_gate(r.endpoint, settings, validator), methods=["POST"])
           for r in create_jsonrpc_routes(handler, rpc_url=A2A_PATH, context_builder=_ContextBuilder(),
                                          enable_v0_3_compat=True)]
    before = len(server.router.routes)
    # The card at the root (the A2A convention) and next to the endpoint, where clients given the /a2a URL look.
    cards = create_agent_card_routes(card) + create_agent_card_routes(card, card_url=A2A_PATH + AGENT_CARD_WELL_KNOWN_PATH)
    add_a2a_routes_to_fastapi(server, agent_card_routes=cards, jsonrpc_routes=rpc)
    added = server.router.routes[before:]
    del server.router.routes[before:]
    server.router.routes[0:0] = added
