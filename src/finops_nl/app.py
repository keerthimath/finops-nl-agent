"""Chainlit entrypoint:  chainlit run src/finops_nl/app.py"""
from __future__ import annotations

import chainlit as cl

from finops_nl.adx import AdxClient
from finops_nl.agent import Agent, build_system_prompt, make_llm
from finops_nl.authorization import is_admin_user
from finops_nl.config import Settings
from finops_nl.logging_utils import log_event
from finops_nl.session_tokens import AuthExpired, token_store
from finops_nl.tools import QUERY_TOOLS, ToolRunner, load_dashboard_queries, load_references, session_profile

settings = Settings.from_env()
settings.validate()

_provider = None
_exchanger = None
if settings.auth_mode == "obo":
    from finops_nl.auth import ClientAuth, OboExchanger
    from finops_nl.entra_provider import register

    _client_auth = ClientAuth(settings)
    _provider = register(settings, _client_auth)
    _exchanger = OboExchanger(settings, _client_auth)

    @cl.oauth_callback
    def oauth_callback(provider_id: str, token: str, raw_user_data: dict, default_user: cl.User):
        return default_user if provider_id == "entra-obo" else None

_llm = make_llm(settings)
_references = load_references(settings)
_dashboard = load_dashboard_queries(settings)


async def _signed_in_user(token):
    """The Chainlit user for a session token (cookie or bearer), or None."""
    from chainlit.auth import get_current_user
    from fastapi import HTTPException

    try:
        return await get_current_user(token)
    except HTTPException:
        return None


def _is_admin(user) -> bool:
    if settings.auth_mode == "dev":
        return True
    return is_admin_user(user, settings.admin_users, settings.admin_group_ids)


def _register_admin() -> None:
    """GET /admin: per-question performance for users in ADMIN_USERS (see admin.py)."""
    import asyncio

    from chainlit.auth import reuseable_oauth
    from chainlit.server import app as server
    from fastapi import Depends
    from fastapi.responses import HTMLResponse

    from finops_nl.admin import Prices, filter_turns, load_turns, message_page, render_page

    # No local-import annotations: app.py postpones annotations and FastAPI resolves them from module globals.
    async def admin(days: int = 7, user: str = "", problems: str = "", token=Depends(reuseable_oauth)):
        if settings.auth_mode == "obo":
            current = await _signed_in_user(token)
            if current is None:
                return HTMLResponse(message_page("Sign in", "<p>Sign in first: <a href='/'>open the assistant</a>, "
                                                             "then come back.</p>"), 401)
            if not _is_admin(current):
                log_event("admin_denied", user=current.identifier)
                return HTMLResponse(message_page("Not authorized",
                                                 "<p>Not authorized. Ask to be added to ADMIN_USERS.</p>"), 403)
        days = days if days in (1, 7, 30) else 7
        error = ""
        try:
            turns, source = await asyncio.to_thread(load_turns, settings, days)
        except Exception as exc:  # noqa: BLE001 - show the reason on the page (e.g. missing reader role)
            turns, source, error = [], "Log Analytics", f"Could not read logs: {str(exc)[:300]}"
        prices = Prices(settings.price_input, settings.price_cached, settings.price_output)
        return HTMLResponse(render_page(filter_turns(turns, user, bool(problems)), days=days, user=user,
                                        problems=bool(problems), source=source, prices=prices, error=error))

    server.add_api_route("/admin", admin, methods=["GET"], response_class=HTMLResponse)
    # Chainlit's catch-all UI route ("/{full_path:path}") is already registered; put /admin ahead of it.
    server.router.routes.insert(0, server.router.routes.pop())


_register_admin()


def _register_favicon_ico() -> None:
    """GET /favicon.ico: browsers ask for it on pages without an icon link (e.g. JSON); otherwise the UI catch-all
    answers with HTML and the tab shows no icon."""
    from chainlit.config import APP_ROOT
    from chainlit.server import app as server
    from fastapi.responses import FileResponse

    path = f"{APP_ROOT}/public/favicon.svg"

    async def favicon_ico():
        return FileResponse(path, media_type="image/svg+xml")

    server.add_api_route("/favicon.ico", favicon_ico, methods=["GET"], include_in_schema=False)
    server.router.routes.insert(0, server.router.routes.pop())


_register_favicon_ico()


def _register_header_link() -> None:
    """'Admin' in the chat header, for admins only; /admin links back to chat.

    Chainlit has one header for everyone, read by each browser from /project/settings, so a middleware removes the
    link from that response for non-admins. /admin still enforces ADMIN_USERS itself: this only hides the link.
    """
    if settings.auth_mode == "obo" and not settings.admin_users:
        return
    import gzip
    import json

    from chainlit.auth import reuseable_oauth
    from chainlit.config import HeaderLink, config
    from chainlit.server import app as server
    from fastapi.responses import JSONResponse

    links = [x for x in (config.ui.header_links or []) if x.url != "/admin"]
    config.ui.header_links = links + [HeaderLink(name="Admin", display_name="Admin", url="/admin", target="_self",
                                                 icon_url="/public/icons/admin.svg")]

    @server.middleware("http")
    async def hide_admin_link(request, call_next):
        response = await call_next(request)
        if not request.url.path.endswith("/project/settings") or response.status_code != 200:
            return response
        if _is_admin(await _signed_in_user(await reuseable_oauth(request))):
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        if response.headers.get("content-encoding") == "gzip":  # Chainlit compresses inside this middleware
            body = gzip.decompress(body)
        data = json.loads(body)
        ui = data.get("ui") or {}
        ui["header_links"] = [x for x in ui.get("header_links") or [] if x.get("url") != "/admin"] or None
        headers = {k: v for k, v in response.headers.items()
                   if k.lower() not in ("content-length", "content-type", "content-encoding")}
        return JSONResponse(data, status_code=200, headers=headers)


_register_header_link()


def _register_a2a() -> None:
    """A2A_ENABLED: other agents ask questions for a signed-in user (a2a_server.py; user token -> OBO -> RLS)."""
    if not settings.a2a_enabled:
        return
    from chainlit.server import app as server

    from finops_nl.a2a_server import FinOpsExecutor, add_routes

    add_routes(server, settings, FinOpsExecutor(settings, _llm, _references, _dashboard, _exchanger))


_register_a2a()


def _brand() -> None:
    """App name in the browser tab and UI. The logos and favicon are public/logo_{dark,light}.svg and public/favicon.svg."""
    from chainlit.config import config

    config.ui.name = "FinOps Assistant"


_brand()
MAX_CHARTS = 2


def _charts(turn, question: str, currency: str) -> list:
    """Charts for this answer, built from rows the app already has (no LLM tokens).

    Dashboard tiles use their own visual type and title; the model's own queries get a type inferred from the
    result's columns and the question as title. Tiles first, then the latest queries; at most MAX_CHARTS.
    """
    from finops_nl.charts import chart_spec, infer_visual, to_figure

    candidates = []  # (title, result, visual), newest last
    for o in turn.outcomes:
        if o.is_error or not o.result:
            continue
        if o.name == "run_dashboard_query" and o.tile and _dashboard and o.tile in _dashboard.tiles:
            tile = _dashboard.tiles[o.tile]
            candidates.append((True, tile.title, o.result, tile.visual))
        elif o.name == "run_kql":
            visual = infer_visual(o.result)
            if visual:
                title = question.strip().rstrip("?")
                candidates.append((False, title[:80] + ("…" if len(title) > 80 else ""), o.result, visual))
    seen, picked = set(), []
    for is_tile, title, result, visual in sorted(reversed(candidates), key=lambda c: not c[0]):
        key = (visual, tuple(result.columns), len(result.rows), str(result.rows[:3]))
        if key in seen:
            continue
        seen.add(key)
        spec = chart_spec(result, visual, title)
        if spec:
            picked.append(cl.Plotly(name=title, figure=to_figure(spec, currency), display="inline", size="large"))
        if len(picked) == MAX_CHARTS:
            break
    return picked


@cl.set_starters
async def starters():
    return [
        cl.Starter(label="Last month's spend", message="What did we spend last month, by subscription?"),
        cl.Starter(label="Top services", message="What are the top 10 services by cost this month?"),
        cl.Starter(label="Trend", message="Show me the daily cost trend for the last 30 days."),
        cl.Starter(label="Unused commitments", message="How much did we waste on unused reservations last month?"),
        cl.Starter(label="Weekly report", message="Create the weekly cost anomaly report."),
    ]


PINNED_COMMAND = "Pinned"  # composer button: select it and press send to open the pinned-questions panel


@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("history", [])
    cl.user_session.set("unavailable_tiles", set())
    await cl.context.emitter.set_commands([{
        "id": PINNED_COMMAND, "icon": "pin", "description": "Show your pinned questions",
        "button": True, "persistent": False,
    }])


async def _show_pins(identity: str) -> None:
    """Pins are kept in the browser per user (public/elements/*.jsx); the panel reads them there."""
    await cl.ElementSidebar.set_title("Pinned questions")
    await cl.ElementSidebar.set_elements([cl.CustomElement(name="PinnedQuestions", props={"user": identity})],
                                         key="pinned-questions")
    cl.user_session.set("pins_open", True)


async def _close_pins() -> None:
    """Close the pinned-questions panel once a question is sent (a click on a pin sends it as a message)."""
    if cl.user_session.get("pins_open"):
        await cl.ElementSidebar.set_elements([])  # an empty list closes the side panel
        cl.user_session.set("pins_open", False)


async def _adx_for(identity: str) -> AdxClient:
    if settings.auth_mode == "dev":
        return AdxClient(settings, None)
    access = await token_store.get_access_token(identity, refresher=_provider.refresh)
    return AdxClient(settings, lambda: _exchanger.adx_token(access))


@cl.on_message
async def on_message(message: cl.Message):
    user = cl.user_session.get("user")
    identity = user.identifier if user else "dev"
    history = cl.user_session.get("history") or []
    if message.command == PINNED_COMMAND and not message.content.strip():
        await message.remove()  # the empty "Pinned" command message isn't part of the conversation
        await _show_pins(identity)  # no LLM call
        return
    await _close_pins()

    try:
        adx = await _adx_for(identity)
    except AuthExpired:
        await cl.Message("Your session has expired. Please refresh the page and sign in again.").send()
        return

    profile = cl.user_session.get("profile")
    if profile is None:  # once per conversation, one query: currency + the user's subscriptions (RLS applies)
        profile = await session_profile(adx, identity)
        cl.user_session.set("profile", profile)
    currency = profile.currency or "unknown"
    runner = ToolRunner(settings, adx, _references, user=identity, dashboard=_dashboard,
                        unavailable_tiles=cl.user_session.get("unavailable_tiles"), profile=profile)
    agent = Agent(settings, _llm, runner,
                  build_system_prompt(currency=currency, subscriptions=profile.subscriptions))

    streamed: list[cl.Message] = []  # the message the model's text is streaming into, if any

    async def on_text(token: str) -> None:
        if not streamed:
            streamed.append(cl.Message(content=""))
        await streamed[0].stream_token(token)

    async def on_event(kind: str, outcome) -> None:
        if kind == "tool_result" and streamed:  # text streamed before a tool call was interim: drop it
            await streamed.pop().remove()
        if kind == "tool_result" and outcome.name in QUERY_TOOLS and not outcome.repeat:
            # Say where the query came from (a dashboard tile = the team's predefined query, or the assistant's own
            # KQL), and mark failed attempts (the model usually retries) so they don't read like the answer.
            label = outcome.source or "KQL (written by the assistant)"
            async with cl.Step(name=f"{label} (failed)" if outcome.is_error else label, type="tool",
                               show_input="kql") as step:
                step.input = outcome.kql or ""
                step.output = outcome.preview or outcome.content

    log_event("question", user=identity, text=message.content[:1000])
    try:
        turn = await agent.ask(history, message.content, on_event, on_text)
    except AuthExpired as exc:
        if streamed:
            await streamed.pop().remove()
        await cl.Message(str(exc)).send()
        return
    except Exception as exc:  # noqa: BLE001
        log_event("agent_error", user=identity, error=str(exc)[:500])
        if streamed:
            await streamed.pop().remove()
        await cl.Message("Something went wrong while answering. Please try again.").send()
        return
    cl.user_session.set("history", history)
    final = streamed[0] if streamed else cl.Message(content="")
    final.content = turn.answer  # ends the stream; also replaces it when the turn hit a limit
    if not turn.hit_limit:  # charts for dashboard tiles, and let the user keep this question for later
        try:
            charts = _charts(turn, message.content, currency)
        except Exception as exc:  # noqa: BLE001 - a chart must never cost the answer
            log_event("chart_error", user=identity, error=str(exc)[:300])
            charts = []
        final.elements = charts + [cl.CustomElement(name="PinButton", display="inline",
                                                    props={"question": message.content.strip(), "user": identity})]
    await final.send()
