"""Wire protocol between the backend and a persona sandbox.

This module is the contract. It is vendored verbatim into the sandbox runtime
image (see sandbox/runtime/runtime/protocol.py, which imports from here at build
time), so the two sides cannot drift: a change that breaks the contract breaks
both test suites at once.

Shape of a turn:

    POST /v1/persona   bind this sandbox to an attendee (idempotent, once)
    POST /v1/turn      -> SSE stream, terminated by a single `turn.result`

The backend keeps the LangGraph graph; the sandbox executes one turn. Nothing
durable lives in the sandbox, so losing one costs a re-issued turn and nothing
else.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelConfig(BaseModel):
    """Inference endpoint for the persona's ReAct loop.

    Deliberately carries no API key: the key is mounted into the sandbox from a
    Secret at /etc/sandbox/secrets/inference-api-key. A backend that never sends
    the credential cannot leak it through a request log.
    """

    endpoint: str
    model_name: str
    temperature: float = 0.7
    timeout_seconds: int = 60
    # Provider-specific; empty means do not send it. See
    # Settings.INFERENCE_REASONING_EFFORT.
    reasoning_effort: str = ""
    # Caps a turn's spoken output. Unbounded generation stalls turns and, on
    # some providers, overruns the context window into a server error.
    #
    # It has to cover a whole ReAct step, not just the reply: a reasoning model
    # thinks, calls a tool, reads the result, then answers. At 800 the budget
    # was spent before the answer, and a Finance Director that had just queried
    # the warehouse contributed "(no comment this turn)".
    max_tokens: int = 2048
    ignore_tls: bool = False


class TurnLimits(BaseModel):
    """Caps applied inside the sandbox.

    These come from the system_settings table. They were once editable
    in the UI and read by nothing -- the agent hardcoded temperature 0.7 and the
    retrieval tool a limit of 3.
    """

    retrieval_limit: int = 3
    max_evidence_per_message: int = 5
    max_tool_calls_per_turn: int = 6


class PersonaSpec(BaseModel):
    """Everything that makes an attendee behave like a specific person.

    Most of these fields existed on RoleAgent and were persisted and editable
    while reaching no prompt at all. Routing them through here is what makes the
    persona editor mean something.
    """

    display_name: str
    title: str
    department: str
    # The operator's free-text notes for this persona. Formerly used as the
    # whole prompt template, which discarded every other field; it is guidance
    # inside the template now, not a replacement for it.
    guidance: str | None = None
    summary: str | None = None
    seniority: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    kpis: list[str] = Field(default_factory=list)
    objectives: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    risk_tolerance: str | None = None
    tone: list[str] = Field(default_factory=list)
    collaboration_style: str | None = None
    challenge_style: str | None = None
    allowed_shared_library_access: bool = True


class PersonaBindRequest(BaseModel):
    """One-shot bind, sent when a sandbox is acquired for an attendee."""

    agent_id: str
    meeting_id: str
    persona: PersonaSpec
    system_prompt_template: str
    granted_tools: list[str] = Field(default_factory=list)
    model: ModelConfig
    limits: TurnLimits = Field(default_factory=TurnLimits)


class PersonaBindResponse(BaseModel):
    agent_id: str
    # Tools actually registered: the requested grant intersected with what this
    # sandbox's own capability file allows. A backend cannot grant a tool the
    # template does not provide.
    active_tools: list[str]
    refused_tools: list[str] = Field(default_factory=list)


class Attendee(BaseModel):
    id: str
    display_name: str
    title: str
    department: str


class Utterance(BaseModel):
    speaker_id: str
    display_name: str
    title: str
    content: str


class TurnRequest(BaseModel):
    """Ask the bound persona to take one turn.

    `turn_key` makes this idempotent. LangGraph replays the last uncompleted
    node after a crash-resume, which would otherwise re-invoke the model and
    re-run tools.
    """

    turn_key: str
    objective: str = ""
    agenda: str = ""
    brief: str = ""
    expectations: str = ""
    attendees: list[Attendee] = Field(default_factory=list)
    transcript: list[Utterance] = Field(default_factory=list)
    directive: str = ""


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    id: str
    name: str
    ok: bool
    summary: str = ""
    duration_ms: int = 0
    # Set when a tool was refused rather than attempted. The UI surfaces these
    # in the UI as the visible half of least privilege.
    denied_reason: str | None = None


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class TurnResult(BaseModel):
    """Terminal SSE event. Everything the backend needs from a turn."""

    turn_key: str
    agent_id: str
    public_content: str
    private_reasoning: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)


class TurnError(BaseModel):
    code: Literal["tool_denied", "model_timeout", "not_bound", "internal"]
    message: str


# --- SSE envelope -----------------------------------------------------------
#
# Each `data:` line is one of these, discriminated on `type`. Deltas are
# advisory and may be dropped; `turn.result` or `turn.error` is authoritative
# and always terminates the stream.

EventType = Literal[
    "thought.delta",
    "speech.delta",
    "tool.call",
    "tool.result",
    "turn.result",
    "turn.error",
]


class TurnEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EventType
    text: str | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    result: TurnResult | None = None
    error: TurnError | None = None

    def to_sse(self) -> str:
        return f"data: {self.model_dump_json(exclude_none=True)}\n\n"
