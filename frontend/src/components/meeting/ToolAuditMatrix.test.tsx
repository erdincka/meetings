import { describe, expect, it, vi } from "vitest"
import { render, screen, within } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

import ToolAuditMatrix from "@/components/meeting/ToolAuditMatrix"
import type { EventLogItem } from "@/store"

/**
 * The audit matrix is the screen the whole demo builds toward, so the cases
 * worth pinning are the ones where a wrong render would misrepresent the
 * security story rather than merely look untidy:
 *
 *  - a refusal must never be shown as an ordinary unused grant
 *  - a refusal must survive a later success on the same tool, because a
 *    persona that was refused once was refused, and averaging that away
 *    flatters the system in exactly the direction that matters
 */

vi.mock("@/lib/api-client", () => ({
  apiClient: {
    get: vi.fn(async () => ({
      all_tools: ["retrieve_documents", "run_python_analysis"],
      attendees: [
        {
          agent_id: "quant-1",
          display_name: "Ada Finance",
          title: "Finance Director",
          profile: "quant",
          profile_description: "Metrics plus model-authored analysis.",
          granted_tools: ["retrieve_documents", "run_python_analysis"],
          can_execute_code: true,
        },
        {
          agent_id: "counsel-1",
          display_name: "Grace Counsel",
          title: "General Counsel",
          profile: "counsel",
          profile_description: "Baseline plus deterministic policy checks.",
          granted_tools: ["retrieve_documents"],
          can_execute_code: false,
        },
      ],
    })),
  },
  UNAUTHENTICATED_EVENT: "meetings:unauthenticated",
}))

function renderMatrix(eventLog: EventLogItem[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <ToolAuditMatrix meetingId="m-1" eventLog={eventLog} />
    </QueryClientProvider>
  )
}

const spoke = (agentId: string, audit: object[]): EventLogItem => ({
  type: "agent_spoke",
  agent_id: agentId,
  tool_audit: audit,
})

describe("the capability matrix", () => {
  it("lists every attendee with the profile the cluster resolved", async () => {
    renderMatrix([])

    expect(await screen.findByText("Finance Director")).toBeInTheDocument()
    expect(screen.getByText("quant")).toBeInTheDocument()
    expect(screen.getByText("General Counsel")).toBeInTheDocument()
    expect(screen.getByText("counsel")).toBeInTheDocument()
  })

  it("renders a denial distinctly from an unused grant", async () => {
    renderMatrix([
      spoke("quant-1", [
        { agent_id: "quant-1", tool: "run_python_analysis", ok: false, denied_reason: "RBAC" },
      ]),
    ])

    await screen.findByText("Finance Director")
    const row = screen.getByText("Finance Director").closest("tr")!
    // A refusal is the one cell an operator must be able to find at a glance.
    expect(within(row).getByText(/Refused by the Kubernetes API server/i)).toBeDefined()
  })

  it("keeps a denial visible after a later success on the same tool", async () => {
    renderMatrix([
      spoke("quant-1", [
        { agent_id: "quant-1", tool: "run_python_analysis", ok: false, denied_reason: "RBAC" },
      ]),
      spoke("quant-1", [{ agent_id: "quant-1", tool: "run_python_analysis", ok: true }]),
    ])

    await screen.findByText("Finance Director")
    const row = screen.getByText("Finance Director").closest("tr")!
    expect(within(row).getByText(/Refused by the Kubernetes API server/i)).toBeDefined()
  })

  it("says plainly that a persona without the grant is refused code execution", async () => {
    renderMatrix([])

    expect(await screen.findByText(/Refused code execution by RBAC/i)).toBeInTheDocument()
  })
})
