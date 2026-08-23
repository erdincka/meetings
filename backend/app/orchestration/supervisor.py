import asyncio
from typing import Any

import structlog
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.core.config import settings
from app.orchestration.prompts import DEFAULT_SUPERVISOR_PROMPT
from app.orchestration.recovery import recover_speaker_id
from app.orchestration.state import MeetingState, public_transcript

logger = structlog.get_logger(__name__)

# Enough for an ID plus a short justification.
SUPERVISOR_MAX_TOKENS = 300


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

    # 2.5 Dynamic Schema for better model adherence
    # We create a dynamic Literal type for the next_speaker field
    from pydantic import create_model

    # Create a dynamic model where next_speaker is restricted to valid_ids
    # This helps models like Gemini/OpenAI stick to the allowed options
    DynamicSupervisorDecision = create_model(
        "SupervisorDecision",
        next_speaker=(str, Field(description=f"Selection from: {', '.join(valid_ids)}")),
        reasoning=(str, Field(description="Detailed explanation for this choice.")),
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

    chain = prompt | llm.with_structured_output(DynamicSupervisorDecision)

    # 3. Execution with Retry
    for attempt in range(3):
        try:
            logger.info("supervisor_invoking_llm", meeting_id=meeting_id, attempt=attempt)
            # Use only public conversational history (chatter) to decide next speaker
            historical_messages = list(public_transcript(state.get("messages", [])))

            decision = await chain.ainvoke(
                {
                    "objective": state.get("objective", ""),
                    "agenda": state.get("agenda", ""),
                    "messages": historical_messages,
                }
            )

            if decision is None:
                raise ValueError("LLM returned empty or malformed structured output.")

            # with_structured_output returns a model instance for most
            # providers but a plain dict for some; handle both.
            if isinstance(decision, dict):
                next_speaker = str(decision.get("next_speaker", "")).strip()
                reasoning = str(decision.get("reasoning", ""))
            else:
                next_speaker = str(decision.next_speaker).strip()
                reasoning = str(decision.reasoning)

            # Small models routinely answer with a name or title instead of
            # the UUID they were asked for. recover_speaker_id() resolves the
            # unambiguous near-misses and returns FINISH otherwise, rather than
            # routing the turn to an arbitrary attendee.
            # A name we cannot resolve is not the same as a decision to stop.
            # Both used to collapse to FINISH, so a single hallucinated name --
            # "Ben", nobody in the room -- ended the meeting at turn 0 and the
            # transcript came out empty. Retry instead, and if the model never
            # produces a usable id, fall back to whoever has not spoken yet.
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
