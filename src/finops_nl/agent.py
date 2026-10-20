"""Tool-use loop around the Anthropic Messages API (OpenAI is adapted to the same message shape)."""
from __future__ import annotations

import datetime as dt
import json
import time
from types import SimpleNamespace
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from .config import Settings
from .logging_utils import log_event
from .tools import QUERY_TOOLS, ToolOutcome, ToolRunner, load_prompt

EventCallback = Callable[[str, Any], Awaitable[None]]
TextCallback = Callable[[str], Awaitable[None]]


class LLM(Protocol):
    async def create(self, *, model: str, system: list[dict], tools: list[dict],
                     messages: list[dict], max_tokens: int, on_text: TextCallback | None = None) -> Any:
        """With on_text, stream: call it with each text delta as it arrives, then return the full response."""


FOUNDRY_TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"


class AnthropicLLM:
    # Current Claude models think by default and thinking counts against max_tokens; the agent's 2000 would
    # often end the turn before the tool call or answer.
    MIN_MAX_TOKENS = 16_000
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, api_key: str | None = None, client: Any = None, effort: str = "", fallbacks: bool = False):
        """effort: output_config.effort ("" = the model's default). fallbacks: on a safety refusal, the Anthropic
        API re-runs the request on a model chosen by refusal category (beta; not on Foundry)."""
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=api_key)
        self._client = client
        self._effort = effort
        self._fallbacks = fallbacks

    async def create(self, *, model, system, tools, messages, max_tokens, on_text=None):
        # No temperature: current models reject non-default sampling values.
        kw = dict(model=model, system=system, tools=tools, messages=messages,
                  max_tokens=max(max_tokens, self.MIN_MAX_TOKENS))
        if self._effort:
            kw["output_config"] = {"effort": self._effort}
        api = self._client.messages
        if self._fallbacks:
            api = self._client.beta.messages
            kw.update(betas=[self.FALLBACK_BETA], fallbacks="default")
        if on_text is None:
            return await api.create(**kw)
        async with api.stream(**kw) as stream:
            async for delta in stream.text_stream:
                await on_text(delta)
            return await stream.get_final_message()


class OpenAILLM:
    """OpenAI Chat Completions behind the Anthropic-shaped interface the agent loop uses.

    Converts Anthropic system/tools/messages to OpenAI's format and the reply back into `text` and
    `tool_use` blocks, so history stays in one format whichever provider is configured.
    """

    # Reasoning models count hidden reasoning against this budget; the agent's 2000 would often leave no answer.
    MIN_COMPLETION_TOKENS = 16_000

    def __init__(self, api_key: str | None = None, client: Any = None, reasoning_effort: str = ""):
        if client is None:
            import openai

            client = openai.AsyncOpenAI(api_key=api_key)
        self._client = client
        # Hidden reasoning dominates latency and output tokens; "low" is enough to pick tiles and write KQL.
        self._extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}

    async def create(self, *, model, system, tools, messages, max_tokens, on_text=None):
        kw = dict(
            **self._extra,
            model=model,
            messages=to_openai_messages(system, messages),
            tools=[{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"],
            }} for t in tools],
            max_completion_tokens=max(max_tokens, self.MIN_COMPLETION_TOKENS),
        )
        if on_text is None:
            resp = await self._client.chat.completions.create(**kw)
            msg = resp.choices[0].message
            calls = [(c.id, c.function.name, c.function.arguments) for c in msg.tool_calls or []]
            return _blocks(msg.content, calls, _openai_usage(getattr(resp, "usage", None)))
        stream = await self._client.chat.completions.create(**kw, stream=True, stream_options={"include_usage": True})
        text: list[str] = []
        calls: dict[int, dict] = {}
        usage = None
        async for chunk in stream:
            usage = getattr(chunk, "usage", None) or usage  # the last chunk carries usage, with no choices
            for choice in chunk.choices or []:
                delta = choice.delta
                if delta.content:
                    text.append(delta.content)
                    await on_text(delta.content)
                for tc in delta.tool_calls or []:  # tool calls arrive in pieces, keyed by index
                    c = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    c["id"] = tc.id or c["id"]
                    if tc.function:
                        c["name"] += tc.function.name or ""
                        c["args"] += tc.function.arguments or ""
        return _blocks("".join(text), [(c["id"], c["name"], c["args"]) for _, c in sorted(calls.items())],
                       _openai_usage(usage))


def _blocks(text: str | None, calls: list[tuple[str, str, str]], usage: dict) -> SimpleNamespace:
    content = [SimpleNamespace(type="text", text=text)] if text else []
    for call_id, name, arguments in calls:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}  # the tool reports the missing argument and the model retries
        content.append(SimpleNamespace(type="tool_use", id=call_id, name=name, input=args))
    return SimpleNamespace(content=content, usage=usage)


def _openai_usage(u: Any) -> dict:
    if u is None:
        return {}
    prompt, completion = getattr(u, "prompt_tokens_details", None), getattr(u, "completion_tokens_details", None)
    return {
        "input": getattr(u, "prompt_tokens", 0) or 0,
        "cached": getattr(prompt, "cached_tokens", 0) or 0,
        "output": getattr(u, "completion_tokens", 0) or 0,
        "reasoning": getattr(completion, "reasoning_tokens", 0) or 0,
    }


def usage_of(resp: Any) -> dict:
    """Token usage as {input (incl. cached), cached, output (incl. reasoning), reasoning}, for either provider."""
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    if isinstance(u, dict):
        return u
    cached = getattr(u, "cache_read_input_tokens", 0) or 0
    written = getattr(u, "cache_creation_input_tokens", 0) or 0
    return {"input": (getattr(u, "input_tokens", 0) or 0) + cached + written, "cached": cached,
            "output": getattr(u, "output_tokens", 0) or 0, "reasoning": 0}


def to_openai_messages(system: list[dict], messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": "\n\n".join(b["text"] for b in system)}] if system else []
    for m in messages:
        if isinstance(m["content"], str):
            out.append({"role": m["role"], "content": m["content"]})
        elif m["role"] == "user":  # tool results
            for b in m["content"]:
                text = b["content"]
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                            "content": f"ERROR: {text}" if b.get("is_error") else text})
        else:
            text = "\n\n".join(b["text"] for b in m["content"] if b["type"] == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                     for b in m["content"] if b["type"] == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    return out


def make_llm(settings: Settings) -> LLM:
    """Claude via the Anthropic API (ANTHROPIC_API_KEY) or Microsoft Foundry; OpenAI (OPENAI_API_KEY); or an
    Azure OpenAI deployment in Foundry (AZURE_OPENAI_*), with Entra ID (default) or a key.

    Foundry with FOUNDRY_AUTH=entra uses DefaultAzureCredential: the Container App's managed identity
    (set AZURE_CLIENT_ID for a user-assigned one) or `az login` locally. No LLM secret is stored.
    FOUNDRY_AUTH=key reads ANTHROPIC_FOUNDRY_API_KEY.
    """
    if settings.llm_provider == "anthropic":
        return AnthropicLLM(effort=settings.anthropic_effort, fallbacks=True)
    if settings.llm_provider == "openai":
        return OpenAILLM(reasoning_effort=settings.openai_reasoning_effort)
    if settings.llm_provider == "azure_openai":
        from openai import AsyncAzureOpenAI

        kw = dict(azure_endpoint=settings.azure_openai_endpoint, api_version=settings.azure_openai_api_version)
        if settings.azure_openai_auth == "key":
            kw["api_key"] = settings.azure_openai_api_key
        else:  # the app's managed identity (Cognitive Services User on the resource) or `az login` locally
            from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

            kw["azure_ad_token_provider"] = get_bearer_token_provider(DefaultAzureCredential(), FOUNDRY_TOKEN_SCOPE)
        return OpenAILLM(client=AsyncAzureOpenAI(**kw), reasoning_effort=settings.openai_reasoning_effort)
    from anthropic import AsyncAnthropicFoundry

    if settings.foundry_auth == "key":
        return AnthropicLLM(client=AsyncAnthropicFoundry(
            resource=settings.foundry_resource, api_key=settings.foundry_api_key,
        ), effort=settings.anthropic_effort)
    from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

    token_provider = get_bearer_token_provider(DefaultAzureCredential(), FOUNDRY_TOKEN_SCOPE)
    return AnthropicLLM(client=AsyncAnthropicFoundry(
        resource=settings.foundry_resource, azure_ad_token_provider=token_provider,
    ), effort=settings.anthropic_effort)


def subscriptions_line(subscriptions: list[tuple[str, float]] | None, max_listed: int = 50) -> str:
    """The user's subscriptions for the prompt: a list the model maps names to itself, so no lookups are needed."""
    if not subscriptions:
        return "Subscriptions: not loaded; resolve subscription names with `find_names`."
    names = [n for n, _ in subscriptions]
    if len(names) == 1:
        return f'The user can see one subscription, "{names[0]}": all their cost is in it, so no subscription filter is needed.'
    if len(names) > max_listed:
        return f"The user can see {len(names)} subscriptions: resolve subscription names with `find_names`."
    return ("The user's subscriptions (exact SubAccountName, costliest first): "
            + ", ".join(f'"{n}"' for n in names) + ". Map what the user says to these yourself.")


def build_system_prompt(today: dt.date | None = None, currency: str | None = None,
                        subscriptions: list[tuple[str, float]] | None = None) -> list[dict]:
    """Stable for a whole conversation, so providers can cache it (and the tools before it) as a prefix."""
    today = today or dt.date.today()
    text = (load_prompt("system.md").replace("{today}", today.isoformat())
            .replace("{currency}", currency or "unknown")
            .replace("{subscriptions}", subscriptions_line(subscriptions)))
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


@dataclass
class AgentTurn:
    answer: str
    kql: list[str] = field(default_factory=list)
    outcomes: list[ToolOutcome] = field(default_factory=list)
    iterations: int = 0
    hit_limit: bool = False
    usage: dict = field(default_factory=lambda: {"input": 0, "cached": 0, "output": 0, "reasoning": 0})
    llm_ms: int = 0

    @property
    def tokens(self) -> int:
        return self.usage["input"] + self.usage["output"]


def _block_to_dict(b: Any) -> dict:
    if b.type == "text":
        return {"type": "text", "text": b.text}
    if b.type == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if hasattr(b, "model_dump"):
        # thinking / redacted_thinking (and fallback markers): sent back exactly as received. Claude checks
        # them against the conversation that produced them, so they must not be altered or dropped mid-turn.
        return b.model_dump(mode="json", exclude_none=True)
    raise ValueError(f"Unsupported content block: {b.type}")


def _call_note(name: str, args: dict) -> str:
    if name == "run_kql":
        kql = str(args.get("kql", ""))
        return "run_kql: " + (kql[:400] + " ..." if len(kql) > 400 else kql)
    return f"{name} {json.dumps(args, separators=(',', ':'))}"


def _query_record(name: str, args: dict, outcome: ToolOutcome) -> dict:
    """One query in the per-question log record the admin page shows."""
    rec: dict[str, Any] = {"tool": name, "ok": not outcome.is_error}
    if name == "run_dashboard_query":
        rec["tile"] = args.get("tile")
        if args.get("parameters"):
            rec["params"] = args["parameters"]
    else:
        rec["kql"] = str(args.get("kql", ""))[:1500]  # enough to review most queries on /admin
    if outcome.result is not None:
        rec["rows"], rec["ms"] = outcome.result.row_count, outcome.result.elapsed_ms
    if outcome.is_error:
        rec["error"] = outcome.content.replace("ADX error: Request is invalid and cannot be processed: ", "")[:240]
    return rec


def compact_turn(question: str, answer: str, calls: list[str]) -> list[dict]:
    """A finished turn as it is kept for follow-ups: the question, the answer, and one line per query that ran.

    Tool results are dropped: they are most of the tokens and the answer already summarizes them.
    """
    content = [{"type": "text", "text": answer}]
    if calls:
        content.append({"type": "text", "text": "(Context for follow-up questions, not part of the answer. "
                        "Queries run for this answer: " + " | ".join(calls) + ")"})
    return [{"role": "user", "content": question}, {"role": "assistant", "content": content}]


def trim_history(messages: list[dict], max_turns: int) -> list[dict]:
    """Keep the last N user questions (and everything after them) without splitting tool pairs."""
    starts = [i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], str)]
    if len(starts) <= max_turns:
        return messages
    return messages[starts[-max_turns]:]


class Agent:
    def __init__(self, settings: Settings, llm: LLM, tools: ToolRunner, system: list[dict] | None = None):
        self._s = settings
        self._llm = llm
        self._tools = tools
        self._system = system if system is not None else build_system_prompt()

    async def ask(self, history: list[dict], question: str, on_event: EventCallback | None = None,
                  on_text: TextCallback | None = None) -> AgentTurn:
        """Mutates `history` in place so the caller keeps conversation state (compacted, see compact_turn).

        on_text streams the model's text as it is written. Text in a round that then calls tools is interim; the
        caller learns that from the following "tool_result" event.
        """
        prior = trim_history(history, self._s.max_history_turns)
        messages = prior + [{"role": "user", "content": question}]
        turn = AgentTurn(answer="")
        calls: list[str] = []
        queries: list[dict] = []
        other_tools: dict[str, int] = {}
        started = time.monotonic()
        answered = False
        # Fixed for the whole turn: Claude rejects replayed thinking once the tool list changes (e.g. a tile
        # marked unavailable mid-turn), and an unchanged prefix keeps the prompt cache warm.
        tools = self._tools.definitions()
        for i in range(1, self._s.max_tool_iterations + 1):
            if turn.tokens >= self._s.max_turn_tokens:
                break  # per-question budget spent
            turn.iterations = i
            t0 = time.monotonic()
            resp = await self._llm.create(
                model=self._s.model, system=self._system,
                tools=tools, messages=messages, max_tokens=2000,
                **({"on_text": on_text} if on_text else {}),
            )
            turn.llm_ms += int((time.monotonic() - t0) * 1000)
            for k, v in usage_of(resp).items():
                turn.usage[k] = turn.usage.get(k, 0) + v
            messages.append({"role": "assistant", "content": [_block_to_dict(b) for b in resp.content]})
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                turn.answer = "".join(b.text for b in resp.content if b.type == "text").strip()
                answered = True
                break
            results = []
            for tu in tool_uses:
                outcome = await self._tools.run(tu.name, dict(tu.input or {}))
                turn.outcomes.append(outcome)
                if outcome.kql and tu.name in QUERY_TOOLS:
                    turn.kql.append(outcome.kql)
                if tu.name in QUERY_TOOLS and not outcome.repeat:
                    queries.append(_query_record(tu.name, dict(tu.input or {}), outcome))
                    if not outcome.is_error:
                        calls.append(_call_note(tu.name, dict(tu.input or {})))
                else:
                    other_tools[tu.name] = other_tools.get(tu.name, 0) + 1
                if on_event:
                    await on_event("tool_result", outcome)
                results.append({
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": outcome.content, "is_error": outcome.is_error,
                })
            messages.append({"role": "user", "content": results})
        if not answered:
            turn.hit_limit = True
            turn.answer = (
                "I couldn't get to a reliable answer within the allowed number of steps. "
                "Try narrowing the question (a time range, a subscription, or a service)."
            )
        history[:] = prior + compact_turn(question, turn.answer, calls)
        log_event(
            "agent_turn", user=getattr(self._tools, "user", "unknown"), question=question[:500],
            model=self._s.model, iterations=turn.iterations, queries=len(turn.kql), hit_limit=turn.hit_limit,
            ms=int((time.monotonic() - started) * 1000), llm_ms=turn.llm_ms,
            input_tokens=turn.usage["input"], cached_tokens=turn.usage["cached"],
            output_tokens=turn.usage["output"], reasoning_tokens=turn.usage["reasoning"],
            answer_chars=len(turn.answer), query_details=queries, other_tools=other_tools,
        )
        return turn
