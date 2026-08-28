import asyncio
import os
from typing import Any

import structlog
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.core.config import settings
from app.orchestration.llm_call import structured_call
from app.orchestration.prompts import DEFAULT_SUPERVISOR_PROMPT
from app.orchestration.recovery import recover_speaker_id, salvage_decision
from app.orchestration.state import MeetingState, public_transcript

logger = structlog.get_logger(__name__)

# The chair's output budget.
#
# 300 was enough for "an ID plus a short justification" and nothing else, which
# is true only of models that answer immediately. Models that emit reasoning
# before the tool call spend the budget thinking and get cut off mid-call: the
# provider returns finish_reason="length" with an incomplete tool call, which
# arrives here as *no* tool call and empty content. Three retries later the
# chair gives up and the meeting ends at turn 0, with nothing in an error state
# anywhere -- every HTTP request was a clean 200.
#
# It scales with the room: more attendees means a longer list to weigh, so a
# budget that fits a two-person demo silently fails a four-person meeting. The
# cap still exists, because an uncapped small model will ramble past the context
# window into a provider 500, but it is now far enough above the working set to
# leave room for reasoning.
SUPERVISOR_MAX_TOKENS = int(os.getenv("SUPERVISOR_MAX_TOKENS", "1500"))


class SupervisorDecision(BaseModel):
    next_speaker: str = Field(
        description="The ID of the next agent to speak, or 'FINISH' to end the meeting."
    )
    reasoning: str = Field(description="Why this speaker was chosen.")


def _first_unheard(state: MeetingState, attendees: dict[str, Any]) -> str | None:
    """Pick the first attendee who has not spoken yet.

    Used only when the supervisor cannot name a valid speaker. Ending the
    meeting is the worse answer while people are still waiting for a turn.
    """
    spoken = {
        str(m.additional_kwargs.get("agent_id", ""))
        for m in state.get("messages", [])
        if getattr(m, "additional_kwargs", None)
    }
    for agent_id in attendees:
        if agent_id not in spoken:
            return agent_id
    return None


async def supervisor_node(state: MeetingState, config: RunnableConfig) -> dict[str, Any]:
    """Decides the next speaker or if the meeting should finish."""
    current_turn = state.get("current_turn", 0)
    turn_limit = state.get("turn_limit", 50)
    meeting_id = state.get("meeting_id")

    logger.info(
        "supervisor_running", curr_turn=current_turn, limit=turn_limit, meeting_id=meeting_id
    )

    # 1. Termination Checks
    is_terminated = state.get("stop_requested") or state.get("terminated")
    is_limit_reached = current_turn >= turn_limit

    if is_terminated or is_limit_reached:
        reason = (
            "Meeting terminated by user request."
            if is_terminated
            else f"Meeting reached turn limit ({turn_limit})."
        )
        logger.info("supervisor_concluding", reason=reason)
        content = "[Supervisor] Concluded the meeting."
        return {
            "next_speaker": "FINISH",
            "reasoning": reason,
            "messages": [AIMessage(content=content)],
            "event_log": [
                {
                    "type": "supervisor_spoke",
                    "agent_id": "supervisor",
                    "content": content,
                    "reasoning": reason,
                    "private_reasoning": reason,
                    "is_conclusion": True,
                }
            ],
            "final_summary": reason,
        }

    # 2. LLM Setup
    model_settings = config["configurable"]["model_settings"]
    attendees = config["configurable"]["attendees"]

    llm_params = {
        # Ollama and other local providers serve an OpenAI-compatible API with
        # no authentication, but the client will not construct without a key.
        "api_key": model_settings.inference_api_key or "not-required",
        "base_url": model_settings.inference_endpoint,
        "model": model_settings.inference_model_name,
        # Low temperature: the supervisor emits structured output, and drift
        # here costs a whole turn.
        "temperature": model_settings.supervisor_temperature,
        "timeout": settings.LLM_TIMEOUT_SECONDS,
        # A speaker decision is an ID and a sentence. Without a cap, a small
        # model can ramble for hundreds of tokens and, on some providers, run
        # past the context window into a 500. Observed on Ollama: generation
        # reached 320+ tokens before the server errored.
        "max_tokens": SUPERVISOR_MAX_TOKENS,
        # The OpenAI client retries 5xx twice by default, silently turning a 90s
        # timeout into nearly six minutes of apparent hang. This node does its
        # own bounded retry loop with logging, so leave the transport alone.
        "max_retries": 0,
    }

    # Without this the chair never answers: gemma4 spends the whole 300-token
    # budget reasoning and returns truncated JSON, which surfaces as three
    # failed attempts and a meeting that ends before it starts.
    if settings.SUPERVISOR_REASONING_EFFORT:
        llm_params["reasoning_effort"] = settings.SUPERVISOR_REASONING_EFFORT

    if getattr(model_settings, "inference_ignore_tls", False):
        from app.core.network import get_http_client, get_sync_http_client

        llm_params["http_client"] = get_sync_http_client(ignore_tls=True)
        llm_params["http_async_client"] = get_http_client(ignore_tls=True)

    llm = ChatOpenAI(**llm_params)

    attendee_options = []
    for agent_id, agent in attendees.items():
        attendee_options.append(
            f"ID: '{agent_id}' | Name: {agent.display_name} | "
            f"Role: {agent.title} | Department: {agent.department}"
        )

    valid_ids = list(attendees.keys()) + ["FINISH"]

    # 2.5 Dynamic schema, so the choice is constrained by the schema rather than
    # only asked for in prose.
    #
    # `next_speaker` is a Literal of the actual ids, which is what the two
    # comments here always claimed and what the code did not do: the field was a
    # bare `str` whose description listed the ids, leaving the model free to
    # return anything at all. Structured-output backends turn a Literal into a
    # JSON Schema `enum` and constrain decoding to it; a described `str`
    # constrains nothing.
    #
    # The failure that motivated fixing it: a small model wrote a paragraph of
    # reasoning about one attendee and returned the id of a different one --
    # usually the previous speaker, whose id it had just seen in context. The
    # meeting then heard from the same person twice while the chair's stated
    # reason named somebody else, which reads as the chair contradicting itself.
    # recover_speaker_id() cannot catch that: the wrong id is a perfectly valid
    # id, so there is nothing to recover.
    from typing import Literal

    from pydantic import create_model

    DynamicSupervisorDecision = create_model(
        "SupervisorDecision",
        next_speaker=(
            Literal[tuple(valid_ids)],  # type: ignore[valid-type]
            Field(description=f"Selection from: {', '.join(valid_ids)}"),
        ),
        # Optional, with a default. It is explanatory text for the UI, not part
        # of the routing decision -- so it must never be the reason a valid
        # decision is discarded. Required, it was: a model that answered
        # {"next_speaker": "<a real id>"} and nothing else failed Pydantic
        # validation, the attempt was retried, and after three of those the
        # chair gave up and ended the meeting at turn 0. A speaker with no
        # stated reason is worth incomparably more than no meeting.
        reasoning=(str, Field(default="", description="Detailed explanation for this choice.")),
    )

    # 2.6 Prompt Selection & Placeholder replacement
    raw_prompt = getattr(model_settings, "supervisor_prompt", None) or DEFAULT_SUPERVISOR_PROMPT

    # Placeholders: {{OBJECTIVE}}, {{AGENDA}}, {{ATTENDEE_LIST}}
    attendee_list_str = chr(10).join(attendee_options)

    final_sys_prompt = (
        raw_prompt.replace("{{ATTENDEE LIST}}", attendee_list_str)
        .replace("{{OBJECTIVE}}", "{objective}")
        .replace("{{AGENDA}}", "{agenda}")
        # The chair was choosing speakers without knowing what the meeting was
        # convened to produce, which is most of what should drive the choice.
        .replace("{{BRIEF}}", state.get("brief", "") or "None provided.")
        .replace("{{EXPECTATIONS}}", state.get("expectations", "") or "Not specified.")
    )

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", final_sys_prompt),
            ("placeholder", "{messages}"),
        ]
    )

    # Two ways to ask the same question, handed to the ladder in llm_call.
    #
    # Tool calling first: it is the most widely implemented structured mode, and
    # far more portable than `json_schema`, which several providers reject
    # outright. include_raw so a rejected response stays inspectable -- without
    # it every failure arrives as a bare None and "no tool call", "one field
    # missing" and "answered in prose" become indistinguishable.
    #
    # Plain text as the floor. It needs no schema support of any kind, so it
    # works on a provider that implements nothing beyond generating text, and it
    # is what makes this generic rather than tuned to whichever model was tried
    # last.
    def invoke_tools(budget: int):
        bound = llm.bind(max_tokens=budget).with_structured_output(
            DynamicSupervisorDecision, method="function_calling", include_raw=True
        )
        return (prompt | bound).ainvoke(chain_inputs())

    text_instruction = (
        "\n\nReply with a single JSON object and nothing else, in this exact form:\n"
        '{{"next_speaker": "<one ID from the list above, or FINISH>", '
        '"reasoning": "<one sentence>"}}'
    )
    text_prompt = ChatPromptTemplate.from_messages(
        [("system", final_sys_prompt + text_instruction), ("placeholder", "{messages}")]
    )

    def invoke_text(budget: int):
        return (text_prompt | llm.bind(max_tokens=budget)).ainvoke(chain_inputs())

    def chain_inputs() -> dict[str, Any]:
        return {
            "objective": state.get("objective", ""),
            "agenda": state.get("agenda", ""),
            # Only public chatter decides who speaks next.
            "messages": list(public_transcript(state.get("messages", []))),
        }

    # 3. Execution
    for attempt in range(3):
        try:
            logger.info("supervisor_invoking_llm", meeting_id=meeting_id, attempt=attempt)

            outcome = await structured_call(
                invoke_tools=invoke_tools,
                invoke_text=invoke_text,
                salvage=salvage_decision,
                required_field="next_speaker",
                max_tokens=SUPERVISOR_MAX_TOKENS,
                log_context={"meeting_id": meeting_id, "attempt": attempt},
            )

            if not outcome.ok:
                # Every rung's reason, not one word for all of them. This is the
                # difference between "empty or malformed structured output" --
                # which described truncation, a provider refusal, an empty body
                # and an unparseable reply identically -- and a line that names
                # which of those actually happened.
                detail = outcome.failure_reason()
                logger.error(
                    "supervisor_output_unparsed",
                    meeting_id=meeting_id,
                    attempt=attempt,
                    model=model_settings.inference_model_name,
                    detail=detail,
                    attempts=[a.__dict__ for a in outcome.attempts],
                )
                raise ValueError(detail)

            decision = outcome.value or {}
            next_speaker = str(decision.get("next_speaker", "")).strip()
            reasoning = str(decision.get("reasoning", "") or "")

            explicit_finish = next_speaker.strip().upper() == "FINISH"
            resolved = recover_speaker_id(next_speaker, set(valid_ids), attendees)
            if resolved == "FINISH" and not explicit_finish:
                logger.warning("supervisor_id_unresolvable", received=next_speaker, attempt=attempt)
                if attempt < 2:
                    await asyncio.sleep(0.5)
                    continue
                resolved = _first_unheard(state, attendees) or "FINISH"
                logger.warning("supervisor_fell_back_to_unheard", resolved=resolved)
            elif resolved != next_speaker:
                logger.info("supervisor_id_resolved", received=next_speaker, resolved=resolved)
            next_speaker = resolved

            is_finish = next_speaker == "FINISH"
            detail = (
                "Concluded the meeting." if is_finish else f"Selected next speaker: {next_speaker}"
            )
            content = f"[Supervisor] {detail}"

            return {
                "next_speaker": next_speaker,
                "current_turn": current_turn + 1,
                "reasoning": reasoning,
                "messages": [AIMessage(content=content)],
                "event_log": [
                    {
                        "type": "supervisor_spoke",
                        "agent_id": "supervisor",
                        "content": content,
                        "reasoning": reasoning,
                        "private_reasoning": reasoning,
                        "is_conclusion": is_finish,
                    }
                ],
                "final_summary": reasoning if is_finish else None,
            }

        except Exception as e:
            logger.error("supervisor_attempt_failed", error=str(e), attempt=attempt)
            if attempt < 2:
                await asyncio.sleep(0.5)  # Quick backoff
                continue

            # FINAL FALLBACK
            #
            # Before concluding, try the same answer the unresolvable-id path
            # already uses: whoever has not spoken yet. A chair that cannot get
            # a usable reply out of the model is a reason to lose a turn, not a
            # reason to lose the meeting -- and the model failing here says
            # nothing about whether the attendees can still contribute. The
            # provider failure that motivated this returned a one-token empty
            # completion intermittently, killing meetings several turns in that
            # were otherwise going fine.
            unheard = _first_unheard(state, attendees)
            if unheard:
                logger.warning(
                    "supervisor_degraded_to_unheard_speaker",
                    meeting_id=meeting_id,
                    resolved=unheard,
                    error=str(e)[:200],
                )
                note = (
                    "The chair could not reach a decision this turn "
                    f"({str(e)[:160]}); continuing with an attendee who has not spoken."
                )
                return {
                    "next_speaker": unheard,
                    "current_turn": current_turn + 1,
                    "reasoning": note,
                    "messages": [AIMessage(content="[Supervisor] Selected next speaker.")],
                    "event_log": [
                        {
                            "type": "supervisor_spoke",
                            "agent_id": "supervisor",
                            "content": "[Supervisor] Selected next speaker.",
                            "reasoning": note,
                            "private_reasoning": note,
                        },
                        {
                            "type": "supervisor_selected_next_agent",
                            "agent_id": "supervisor",
                            "content": unheard,
                        },
                    ],
                }

            # Everyone has had a turn and the chair still cannot answer. Now
            # concluding is the honest outcome rather than a way to hide a fault.
            logger.error("supervisor_critically_failed_finishing_safe", meeting_id=meeting_id)
            err_msg = (
                f"Supervisor encountered a technical error: {str(e)}. Meeting concluded for safety."
            )
            fallback_content = "[Supervisor] System Error Encountered."
            return {
                "next_speaker": "FINISH",
                "reasoning": err_msg,
                "event_log": [
                    {
                        "type": "supervisor_spoke",
                        "agent_id": "supervisor",
                        "content": fallback_content,
                        "reasoning": err_msg,
                        "private_reasoning": err_msg,
                        "is_conclusion": True,
                    }
                ],
                "final_summary": err_msg,
            }

    # Unreachable in practice -- the loop returns on success and on the final
    # attempt's failure -- but without it the function falls off the end and
    # implicitly returns None, which the graph would treat as an empty state
    # update and route nowhere. Fail closed instead.
    logger.error("supervisor_exhausted_without_decision", meeting_id=meeting_id)
    exhausted = "Supervisor exhausted all attempts without reaching a decision."
    return {
        "next_speaker": "FINISH",
        "reasoning": exhausted,
        "event_log": [
            {
                "type": "supervisor_spoke",
                "agent_id": "supervisor",
                "content": "[Supervisor] System Error Encountered.",
                "reasoning": exhausted,
                "private_reasoning": exhausted,
                "is_conclusion": True,
            }
        ],
        "final_summary": exhausted,
    }
