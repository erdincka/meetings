"""The ReAct loop for one persona, executed inside the sandbox.

This is the code that moved out of the backend. Keeping the whole loop here --
rather than RPC-ing individual tool calls back -- means model-chosen tool
arguments never enter the backend process alongside the database engine, and a
four-step turn costs one round trip instead of four.

Model calls go back out through the backend's proxy, so this pod holds no
provider credential: it authenticates with its own ServiceAccount token and the
backend, which is not sandboxed, supplies the key.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable

import httpx
import structlog
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from .capabilities import resolve_grant
from .persona import build_system_prompt, render_attendee_list
from .protocol import (
    PersonaBindRequest,
    TokenUsage,
    ToolCall,
    ToolResult,
    TurnEvent,
    TurnRequest,
    TurnResult,
)
from .recovery import as_text, split_thought, strip_speaker_prefix
from .tool_guidance import render_tool_guidance
from .tools import build_tools
from .tools.code_exec import DENIED_PREFIX

logger = structlog.get_logger(__name__)


class BoundPersona:
    """A sandbox bound to one attendee for the life of a meeting."""

    def __init__(
        self,
        bind: PersonaBindRequest,
        backend: httpx.AsyncClient,
        token_reader: Callable[[], str] | None = None,
    ) -> None:
        self.bind = bind
        self.backend = backend
        self.active_tools, self.refused_tools = resolve_grant(bind.granted_tools)

        llm_kwargs: dict[str, object] = {
            # The credential is this pod's ServiceAccount token, not a provider
            # key -- `base_url` points at the backend's proxy, which holds the
            # real key and validates this token by TokenReview. There is no
            # provider credential in this pod to send.
            #
            # A placeholder here rather than the token itself: ChatOpenAI would
            # capture it once, and kubelet rotates it out from under a sandbox
            # that outlives its validity. The event hook below re-reads it per
            # request instead.
            "api_key": "sandbox-serviceaccount",
            "base_url": bind.model.endpoint,
            "model": bind.model.model_name,
            "temperature": bind.model.temperature,
            "timeout": bind.model.timeout_seconds,
            # Bounded so a rambling model cannot stall a turn or run past the
            # context window; some providers return 500 rather than truncating.
            "max_tokens": bind.model.max_tokens,
            # The transport's own 5xx retries would multiply the turn timeout
            # invisibly. Failures surface as turn.error instead.
            "max_retries": 0,
        }
        # Reasoning models otherwise spend the turn budget thinking rather than
        # speaking, which both slows a turn and truncates it. Provider-specific,
        # so it is only sent when configured.
        if bind.model.reasoning_effort:
            llm_kwargs["reasoning_effort"] = bind.model.reasoning_effort

        async def _authorize(request: httpx.Request) -> None:
            token = token_reader() if token_reader else ""
            if token:
                request.headers["authorization"] = f"Bearer {token}"

        # Must be async: httpx awaits event hooks on an AsyncClient, so a sync
        # hook returns None and every request dies with "object NoneType can't
        # be used in 'await' expression".
        llm_kwargs["http_async_client"] = httpx.AsyncClient(
            verify=not bind.model.ignore_tls,
            timeout=bind.model.timeout_seconds,
            event_hooks={"request": [_authorize]},
        )
        self.llm = ChatOpenAI(**llm_kwargs)  # type: ignore[arg-type]

    def _build_tools(self) -> list[BaseTool]:
        return build_tools(
            self.active_tools,
            client=self.backend,
            agent_id=self.bind.agent_id,
            meeting_id=self.bind.meeting_id,
            library_access=self.bind.persona.allowed_shared_library_access,
            limit=self.bind.limits.retrieval_limit,
            artifact_writer=self._write_artifact,
        )

    async def _write_artifact(self, *, kind: str, title: str, body: str, mime_type: str) -> str:
        """Persist an artifact produced by a tool, returning its id."""
        response = await self.backend.post(
            "/internal/v1/artifacts",
            json={
                "meeting_id": self.bind.meeting_id,
                "kind": kind,
                "title": title,
                "body": body,
                "mime_type": mime_type,
            },
        )
        response.raise_for_status()
        return str(response.json().get("data", {}).get("id", ""))

    async def run_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
        """Execute one turn, streaming progress and ending with turn.result."""
        started = time.monotonic()
        persona = self.bind.persona

        system_prompt = build_system_prompt(
            self.bind.system_prompt_template,
            persona,
            objective=request.objective,
            agenda=request.agenda,
            brief=request.brief,
            expectations=request.expectations,
            attendee_list=render_attendee_list(request.attendees, self.bind.agent_id),
            tools=render_tool_guidance(self.active_tools),
        )

        history = [HumanMessage(content=u.content) for u in request.transcript]

        # Always close with an explicit instruction to speak.
        #
        # Without it the first speaker receives a system prompt and nothing
        # else, and models reliably respond by asking for the context they were
        # already given rather than opening the meeting. The chair's reasoning
        # is included when the supervisor supplied one.
        cue = request.directive or (
            "It is your turn to speak. Contribute to the meeting now, in "
            "character, addressing the objective and agenda directly. Do not "
            "ask for context; everything you need is above. Do not restate "
            "your role."
        )
        history.append(HumanMessage(content=f"[Chair] {cue}"))

        agent = create_react_agent(self.llm, self._build_tools(), prompt=system_prompt)

        try:
            response = await agent.ainvoke({"messages": history})
        except Exception as exc:
            # Keep the traceback. This path produced an intermittent
            # "object NoneType can't be used in 'await' expression" that was
            # undiagnosable from the message alone, because the frame where the
            # None was awaited never reached the log.
            logger.error(
                "agent_invocation_failed",
                error=f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            raise

        produced = response["messages"][len(history) :]

        tool_calls: list[ToolCall] = []
        tool_results: list[ToolResult] = []
        for message in produced:
            if isinstance(message, AIMessage) and message.tool_calls:
                for call in message.tool_calls:
                    tc = ToolCall(
                        id=str(call.get("id") or uuid.uuid4()),
                        name=str(call.get("name", "unknown")),
                        args=dict(call.get("args") or {}),
                    )
                    tool_calls.append(tc)
                    yield TurnEvent(type="tool.call", tool_call=tc)
            elif isinstance(message, ToolMessage):
                body = as_text(message.content)
                # A cluster denial is not a tool failure: it is a policy
                # decision, and the audit matrix has to tell them apart. An
                # outage shown as a denial would misrepresent the security
                # model; a denial shown as an outage would hide it.
                denied = body.startswith(DENIED_PREFIX)
                tr = ToolResult(
                    id=str(message.tool_call_id or uuid.uuid4()),
                    name=message.name or "unknown",
                    ok=not denied
                    and not body.lower().startswith(("error", "retrieval is unavailable")),
                    summary=body[:500],
                    denied_reason=body[len(DENIED_PREFIX) :].lstrip(": ") if denied else None,
                )
                tool_results.append(tr)
                yield TurnEvent(type="tool.result", tool_result=tr)

        spoken = [m for m in produced if isinstance(m, AIMessage) and as_text(m.content).strip()]
        raw = as_text(spoken[-1].content) if spoken else ""

        public, thought = split_thought(raw)
        public = strip_speaker_prefix(public)
        if not public:
            public = "(no comment this turn)"

        if thought:
            yield TurnEvent(type="thought.delta", text=thought)
        yield TurnEvent(type="speech.delta", text=public)

        usage = TokenUsage()
        for message in produced:
            meta = getattr(message, "usage_metadata", None) or {}
            usage.prompt_tokens += int(meta.get("input_tokens") or 0)
            usage.completion_tokens += int(meta.get("output_tokens") or 0)

        logger.info(
            "turn_complete",
            agent_id=self.bind.agent_id,
            turn_key=request.turn_key,
            tools=len(tool_calls),
            ms=int((time.monotonic() - started) * 1000),
        )

        yield TurnEvent(
            type="turn.result",
            result=TurnResult(
                turn_key=request.turn_key,
                agent_id=self.bind.agent_id,
                public_content=f"[{persona.display_name} - {persona.title}] {public}",
                private_reasoning=thought,
                tool_calls=tool_calls,
                tool_results=tool_results,
                usage=usage,
            ),
        )
