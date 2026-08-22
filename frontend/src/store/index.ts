import { create } from "zustand"

import type { Role } from "@/lib/types"

/** A single event streamed over the meeting WebSocket. */
export interface EventLogItem {
  type: string
  agent_id?: string
  content?: string
  private_reasoning?: string
  reasoning?: string
  raw_content?: string
  name?: string
  timestamp?: string
  is_conclusion?: boolean
  meeting_id?: string
  /** Backend events carry fields the UI does not model yet. */
  [key: string]: unknown
}

export type MeetingStatus =
  | "draft"
  | "queued"
  | "running"
  | "stopping"
  | "completed"
  | "terminated"
  | "failed"

const THINKING_EVENTS = new Set(["agent_thinking", "supervisor_selected_next_agent"])
const SPOKE_EVENTS = new Set(["agent_spoke", "supervisor_spoke"])

export interface MeetingState {
  activeMeetingId: string | null
  status: MeetingStatus | null
  currentTurn: number
  eventLog: EventLogItem[]
  attendees: Record<string, Role>
  objective: string
  typingAgentId: string | null

  setActiveMeetingId: (id: string | null) => void
  setStatus: (status: MeetingStatus | null) => void
  setMeetingData: (data: Partial<MeetingState>) => void
  addEvent: (event: EventLogItem) => void
  setTypingAgentId: (id: string | null) => void
  resetMeeting: () => void
}

const INITIAL = {
  activeMeetingId: null,
  status: null,
  currentTurn: 0,
  eventLog: [] as EventLogItem[],
  attendees: {} as Record<string, Role>,
  objective: "",
  typingAgentId: null,
}

export const useMeetingStore = create<MeetingState>((set) => ({
  ...INITIAL,

  setActiveMeetingId: (id) => set({ activeMeetingId: id }),
  setStatus: (status) => set({ status }),
  setMeetingData: (data) => set((state) => ({ ...state, ...data })),

  addEvent: (event) =>
    set((state) => ({
      eventLog: [...state.eventLog, event],
      currentTurn:
        event.type === "supervisor_selected_next_agent"
          ? state.currentTurn + 1
          : state.currentTurn,
      typingAgentId: THINKING_EVENTS.has(event.type)
        ? (event.agent_id ?? null)
        : SPOKE_EVENTS.has(event.type)
          ? null
          : state.typingAgentId,
    })),

  setTypingAgentId: (id) => set({ typingAgentId: id }),

  resetMeeting: () => set({ ...INITIAL }),
}))
