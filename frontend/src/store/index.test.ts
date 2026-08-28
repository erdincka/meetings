import { beforeEach, describe, expect, it } from "vitest"

import { useMeetingStore } from "@/store"

/**
 * The transcript store folds a stream of backend events into what the meeting
 * view renders. Two of its rules are load-bearing and easy to break silently:
 * the turn counter advances on the chair's selection rather than on an
 * utterance, and the typing indicator has to clear when someone actually
 * speaks -- an indicator stuck on a persona that has finished is the UI saying
 * a turn is still running when it is not.
 */

const store = () => useMeetingStore.getState()

beforeEach(() => {
  store().resetMeeting()
})

describe("turn counting", () => {
  it("advances when the chair selects a speaker", () => {
    store().addEvent({ type: "supervisor_selected_next_agent", agent_id: "a" })
    expect(store().currentTurn).toBe(1)
  })

  it("does not advance on an utterance", () => {
    store().addEvent({ type: "agent_spoke", agent_id: "a" })
    expect(store().currentTurn).toBe(0)
  })

  it("does not advance on a failure, which is not a turn taken", () => {
    store().addEvent({ type: "agent_failed", agent_id: "a" })
    expect(store().currentTurn).toBe(0)
  })
})

describe("the typing indicator", () => {
  it("points at whoever the chair just selected", () => {
    store().addEvent({ type: "supervisor_selected_next_agent", agent_id: "a" })
    expect(store().typingAgentId).toBe("a")
  })

  it("clears once that persona has spoken", () => {
    store().addEvent({ type: "agent_thinking", agent_id: "a" })
    store().addEvent({ type: "agent_spoke", agent_id: "a" })
    expect(store().typingAgentId).toBeNull()
  })

  it("is untouched by events that are neither", () => {
    store().addEvent({ type: "agent_thinking", agent_id: "a" })
    store().addEvent({ type: "artifact_created", agent_id: "b" })
    expect(store().typingAgentId).toBe("a")
  })
})

describe("resetting between meetings", () => {
  it("drops the previous transcript rather than appending to it", () => {
    store().addEvent({ type: "agent_spoke", agent_id: "a" })
    store().setActiveMeetingId("m-1")
    store().resetMeeting()

    expect(store().eventLog).toEqual([])
    expect(store().activeMeetingId).toBeNull()
    expect(store().currentTurn).toBe(0)
  })
})
