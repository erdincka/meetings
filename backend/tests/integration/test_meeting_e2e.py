"""A whole meeting, from a persisted row to a persisted transcript.

Everything real except the two things a test has no business owning: the model
and the cluster. The chair is a scripted decision sequence, the persona
sandboxes are the in-process fake, and everything between them — the graph, the
router, the RPC client, the SSE parsing, the state reducers, the durable
checkpointer, the turn-result cache and the meeting status machine — is the
code that ships.

Unit tests cover each of those against a fake neighbour. What only shows up here
is what happens when they are wired together and asked to survive: a sandbox
lost mid-meeting, a replayed turn, a persona whose tool grant no profile can
satisfy.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from langchain_core.runnables import Runnable
from sqlalchemy import select

from app.models.roles import RoleAgent
from tests.fake_sandbox import FakeSandboxState, build_fake_sandbox

pytestmark = pytest.mark.integration


class _ScriptedChair(Runnable):
    """Stands in for the supervisor's structured-output call.

    Returns a fixed sequence of speaker decisions. The real chair is an LLM and
    is the least deterministic component in the system; scripting it is what
    makes the rest of the path assertable at all.

    A real ``Runnable`` rather than a bare callable, because the supervisor
    composes it with ``prompt | llm``: LangChain wraps a plain callable in a
    ``RunnableLambda`` and invokes it with the formatted prompt as a positional
    argument, which is a confusing way to discover the fake is the wrong shape.
    """

    def __init__(self, decisions: list[dict[str, str]]) -> None:
        self._decisions = list(decisions)
        self.calls = 0

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _ScriptedChair:
        return self

    def invoke(self, _input: Any, config: Any = None, **_kwargs: Any) -> dict[str, str]:
        raise NotImplementedError("the supervisor is async")

    async def ainvoke(self, _input: Any, config: Any = None, **_kwargs: Any) -> dict[str, str]:
        self.calls += 1
        if self._decisions:
            return self._decisions.pop(0)
        return {"next_speaker": "FINISH", "reasoning": "Everyone has spoken."}


@pytest.fixture
async def attendees(db_session, unique: str) -> list[RoleAgent]:
    """Two personas that resolve to different capability profiles."""
    rows = [
        RoleAgent(
            id=uuid.uuid4(),
            display_name=f"Ada Finance {unique}",
            title="Finance Director",
            department="Finance",
            default_tools=["retrieve_documents"],
        ),
        RoleAgent(
            id=uuid.uuid4(),
            display_name=f"Grace Counsel {unique}",
            title="General Counsel",
            department="Legal",
            default_tools=["retrieve_documents"],
        ),
    ]
    for row in rows:
        db_session.add(row)
    await db_session.commit()
    for row in rows:
        await db_session.refresh(row)
    yield rows
    for row in rows:
        await db_session.delete(row)
    await db_session.commit()


@dataclass
class _FakeSandboxRecord:
    claim_name: str
    sandbox_id: str


class _FakeSDK:
    """Stands in for the Agent Sandbox SDK, one level below the manager.

    Patching ``manager.acquire`` would be easier and would test nothing: the
    lease table, the bind-once rule and the eviction retry all live inside it.
    This replaces only the call that would reach a real apiserver, so everything
    above it is the code that ships.
    """

    def __init__(self) -> None:
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.exhausted_for: set[str] = set()

    def create_sandbox(self, *, warmpool: str, pod_labels: dict, **_kwargs: Any):
        from app.core.sandbox_auth import AGENT_LABEL

        agent_id = pod_labels.get(AGENT_LABEL, "")
        if agent_id in self.exhausted_for:
            raise RuntimeError("warm pool exhausted")
        name = f"sandbox-{len(self.created)}"
        self.created.append(name)
        return _FakeSandboxRecord(claim_name=f"claim-{name}", sandbox_id=name)

    def delete_sandbox(self, *, claim_name: str, namespace: str) -> None:
        self.deleted.append(claim_name)


@pytest.fixture
def sandbox_fleet(monkeypatch):
    """The real SandboxManager over a fake SDK, serving the in-process runtime."""
    from app.orchestration import agents as agents_module
    from app.sandbox.manager import SandboxManager, manager

    state = FakeSandboxState()
    transport = httpx.ASGITransport(app=build_fake_sandbox(state))
    sdk = _FakeSDK()

    # The manager is a module-level singleton, so leases would otherwise leak
    # from one test into the next and "reused an existing sandbox" would pass
    # for the wrong reason.
    manager._leases.clear()
    monkeypatch.setattr(SandboxManager, "_sdk_client", lambda self: sdk)
    monkeypatch.setattr(SandboxManager, "base_url_for", lambda self, name: f"http://{name}.test")

    async def enter_with_transport(self):  # type: ignore[no-untyped-def]
        self._http = httpx.AsyncClient(transport=transport, base_url=self.base_url)
        self._owns_http = True
        return self

    monkeypatch.setattr(agents_module.PersonaSandboxClient, "__aenter__", enter_with_transport)

    state.sdk = sdk  # type: ignore[attr-defined]
    yield state
    manager._leases.clear()


@pytest.fixture
def scripted_chair(monkeypatch):
    def _install(decisions: list[dict[str, str]]) -> _ScriptedChair:
        from app.orchestration import supervisor

        chair = _ScriptedChair(decisions)
        monkeypatch.setattr(supervisor, "ChatOpenAI", lambda **_kwargs: chair)
        return chair

    return _install


async def _create_meeting(
    client: httpx.AsyncClient, attendees: list[RoleAgent], turn_limit: int = 3
) -> str:
    from tests.integration.conftest import OPERATOR_HEADERS

    response = await client.post(
        "/api/v1/meetings",
        json={
            "brief": "A supplier defect was found in the Q3 batch.",
            "agenda": "1. Facts 2. Options 3. Decision",
            "objective": "Decide whether to recall.",
            "expectations": "A decision today.",
            "selected_attendee_ids": [str(a.id) for a in attendees],
            "turn_limit": turn_limit,
            "uploaded_brief_docs": [],
        },
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


async def _run(meeting_id: str) -> list[dict[str, Any]]:
    from app.services.meeting_executor import run_meeting_execution

    return [event async for event in run_meeting_execution(meeting_id)]


class TestHappyPath:
    async def test_a_meeting_produces_a_transcript(
        self, client, attendees, sandbox_fleet, scripted_chair
    ) -> None:
        chair = scripted_chair(
            [
                {"next_speaker": str(attendees[0].id), "reasoning": "Facts first."},
                {"next_speaker": str(attendees[1].id), "reasoning": "Legal view."},
                {"next_speaker": "FINISH", "reasoning": "Decision reached."},
            ]
        )
        meeting_id = await _create_meeting(client, attendees)
        events = await _run(meeting_id)

        assert chair.calls >= 2
        assert events[0]["type"] == "meeting_started"
        assert events[-1]["type"] == "meeting_completed", events[-1]

        spoke = [e for e in events if e.get("type") == "agent_spoke"]
        assert len(spoke) == 2, "both selected attendees must have taken a turn"
        assert {e["agent_id"] for e in spoke} == {str(a.id) for a in attendees}

    async def test_the_transcript_is_persisted(
        self, client, db_session, attendees, sandbox_fleet, scripted_chair
    ) -> None:
        """The browser can disconnect; the record has to survive it."""
        from app.models.meetings import Meeting

        scripted_chair([{"next_speaker": str(attendees[0].id), "reasoning": "Go."}])
        meeting_id = await _create_meeting(client, attendees)
        await _run(meeting_id)

        db_session.expire_all()
        row = (
            await db_session.execute(select(Meeting).where(Meeting.id == uuid.UUID(meeting_id)))
        ).scalar_one()
        assert row.status == "completed"
        assert row.meeting_log, "the event log was not written back"

    async def test_one_sandbox_per_persona_not_one_per_turn(
        self, client, attendees, sandbox_fleet, scripted_chair
    ) -> None:
        """The warm pool is sized for speakers, not for turns.

        Claiming per turn drains a pool of two on the third turn of a
        four-person meeting, and every attendee after that pays a cold gVisor
        start or fails outright -- while the leaked claims stay invisible,
        because only the newest sandbox per agent survives into the graph state
        the end-of-meeting release walks.
        """
        scripted_chair(
            [
                {"next_speaker": str(attendees[0].id), "reasoning": "1"},
                {"next_speaker": str(attendees[0].id), "reasoning": "2"},
                {"next_speaker": str(attendees[1].id), "reasoning": "3"},
                {"next_speaker": "FINISH", "reasoning": "done"},
            ],
        )
        meeting_id = await _create_meeting(client, attendees, turn_limit=4)
        await _run(meeting_id)

        assert len(sandbox_fleet.sdk.created) == 2, sandbox_fleet.sdk.created

    async def test_each_persona_is_bound_once_per_meeting(
        self, client, attendees, sandbox_fleet, scripted_chair
    ) -> None:
        """The bind carries the whole persona spec; repeating it costs a round trip."""
        scripted_chair(
            [
                {"next_speaker": str(attendees[0].id), "reasoning": "1"},
                {"next_speaker": str(attendees[0].id), "reasoning": "2"},
                {"next_speaker": "FINISH", "reasoning": "done"},
            ]
        )
        meeting_id = await _create_meeting(client, attendees)
        await _run(meeting_id)

        bound_agents = [bind.agent_id for bind in sandbox_fleet.binds]
        assert len(bound_agents) == len(set(bound_agents)), bound_agents

    async def test_sandboxes_are_handed_back_when_the_meeting_ends(
        self, client, attendees, sandbox_fleet, scripted_chair
    ) -> None:
        scripted_chair([{"next_speaker": str(attendees[0].id), "reasoning": "1"}])
        meeting_id = await _create_meeting(client, attendees)
        await _run(meeting_id)

        assert len(sandbox_fleet.sdk.deleted) == len(sandbox_fleet.sdk.created)


class TestSurvivingALostSandbox:
    """A pod evicted mid-meeting must cost one turn, not the meeting.

    Nothing durable lives in a sandbox, so the recovery is to claim another,
    replay the bind, and re-issue the turn. What must not happen is the meeting
    ending, or the failure being hidden so the transcript shows an unexplained
    silence.
    """

    async def test_an_unavailable_pod_is_reported_and_the_meeting_continues(
        self, client, attendees, sandbox_fleet, scripted_chair
    ) -> None:
        sandbox_fleet.sdk.exhausted_for.add(str(attendees[0].id))
        scripted_chair(
            [
                {"next_speaker": str(attendees[0].id), "reasoning": "Facts first."},
                {"next_speaker": str(attendees[1].id), "reasoning": "Legal view."},
                {"next_speaker": "FINISH", "reasoning": "Done."},
            ]
        )
        meeting_id = await _create_meeting(client, attendees)
        events = await _run(meeting_id)

        failures = [e for e in events if e.get("type") == "agent_failed"]
        assert len(failures) == 1
        assert "warm pool exhausted" in failures[0]["private_reasoning"]

        # The other attendee still spoke, and the meeting still concluded.
        assert any(
            e.get("type") == "agent_spoke" and e["agent_id"] == str(attendees[1].id) for e in events
        )
        assert events[-1]["type"] == "meeting_completed"

    async def test_a_lost_pod_costs_one_turn_and_is_replaced(
        self, client, attendees, sandbox_fleet, scripted_chair, monkeypatch
    ) -> None:
        """An evicted pod must cost a retry, not the turn and not the meeting."""
        from app.orchestration import agents as agents_module

        original = agents_module.PersonaSandboxClient.__aenter__
        calls = {"n": 0}

        async def fail_first_connection(self):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("connection refused")
            return await original(self)

        monkeypatch.setattr(agents_module.PersonaSandboxClient, "__aenter__", fail_first_connection)
        scripted_chair(
            [
                {"next_speaker": str(attendees[0].id), "reasoning": "Go."},
                {"next_speaker": "FINISH", "reasoning": "Done."},
            ]
        )
        meeting_id = await _create_meeting(client, attendees)
        events = await _run(meeting_id)

        # The persona still spoke, from a replacement sandbox.
        assert any(e.get("type") == "agent_spoke" for e in events), events
        assert len(sandbox_fleet.sdk.created) == 2, "no replacement was claimed"
        # And the dead lease was handed back rather than left holding a slot.
        assert sandbox_fleet.sdk.deleted

    async def test_a_failed_turn_leaves_a_visible_gap(
        self, client, attendees, sandbox_fleet, scripted_chair
    ) -> None:
        """Silence and failure must not look the same in the transcript."""
        sandbox_fleet.fail_next = True
        scripted_chair(
            [
                {"next_speaker": str(attendees[0].id), "reasoning": "Go."},
                {"next_speaker": "FINISH", "reasoning": "Done."},
            ]
        )
        meeting_id = await _create_meeting(client, attendees)
        events = await _run(meeting_id)

        failed = [e for e in events if e.get("type") == "agent_failed"]
        assert failed and "scripted failure" in failed[0]["private_reasoning"]


class TestRefusingAnImpossibleMeeting:
    async def test_profile_drift_stops_the_meeting_before_it_starts(
        self, client, db_session, attendees, sandbox_fleet, scripted_chair
    ) -> None:
        """A capability no profile provides is refused up front, with a name.

        Discovering it three turns in, as one participant silently failing, is
        much worse than a precise refusal: the operator would be looking for a
        model problem rather than a configuration one.
        """
        attendees[0].default_tools = ["run_python_analysis", "search_corpus"]
        db_session.add(attendees[0])
        await db_session.commit()

        scripted_chair([{"next_speaker": "FINISH", "reasoning": "n/a"}])
        meeting_id = await _create_meeting(client, attendees)
        events = await _run(meeting_id)

        assert events[-1]["type"] == "error"
        assert attendees[0].display_name in events[-1]["content"]
        assert "profile" in events[-1]["content"].lower()
