"use client"

import { useQuery } from "@tanstack/react-query"
import { Ban, Check, Circle, ShieldCheck, Terminal } from "lucide-react"

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { apiClient } from "@/lib/api-client"
import type { MeetingCapabilities, ToolAuditEntry } from "@/lib/types"
import type { EventLogItem } from "@/store"

/**
 * Which persona may do what, and what actually happened.
 *
 * Granting is only half the picture. A persona can hold a tool in its profile
 * and still be refused at call time -- the interesting cell is the one where
 * the cluster said no. Showing granted, used and denied together is what turns
 * least privilege from a claim in a README into something you can look at.
 */

type CellState = "denied" | "used" | "granted" | "unavailable"

const CELL: Record<CellState, { icon: typeof Check; className: string; label: string }> = {
  denied: {
    icon: Ban,
    className: "text-destructive",
    label: "Refused by the Kubernetes API server",
  },
  used: { icon: Check, className: "text-emerald-500", label: "Granted, and used this meeting" },
  granted: { icon: Circle, className: "text-muted-foreground/40", label: "Granted, not used yet" },
  unavailable: { icon: Circle, className: "text-transparent", label: "Not granted to this persona" },
}

function shortTool(tool: string): string {
  return tool
    .replace(/_/g, " ")
    .replace("retrieve documents", "retrieve")
    .replace("query business metrics", "metrics")
    .replace("run python analysis", "python")
    .replace("check policy compliance", "policy")
    .replace("record action item", "actions")
    .replace("draft artifact", "draft")
    .replace("read artifact", "read")
    .replace("search corpus", "corpus")
}

export default function ToolAuditMatrix({
  meetingId,
  eventLog,
}: {
  meetingId: string
  eventLog: EventLogItem[]
}) {
  const { data } = useQuery<MeetingCapabilities>({
    queryKey: ["meeting_capabilities", meetingId],
    queryFn: () => apiClient.get<MeetingCapabilities>(`/meetings/${meetingId}/capabilities`),
  })

  if (!data || data.attendees.length === 0) return null

  // Fold the audit entries carried on each agent_spoke event into
  // agent -> tool -> outcome. A denial anywhere outranks a success: the point
  // of the matrix is to make refusals visible, not to average them away.
  const outcomes = new Map<string, "used" | "denied">()
  for (const event of eventLog) {
    const audit = event.tool_audit as ToolAuditEntry[] | undefined
    for (const entry of audit ?? []) {
      if (!entry.tool) continue
      const key = `${entry.agent_id}:${entry.tool}`
      if (entry.denied_reason) outcomes.set(key, "denied")
      else if (outcomes.get(key) !== "denied") outcomes.set(key, "used")
    }
  }

  const stateFor = (agentId: string, tool: string, granted: string[]): CellState => {
    if (!granted.includes(tool)) return "unavailable"
    return outcomes.get(`${agentId}:${tool}`) ?? "granted"
  }

  return (
    <TooltipProvider>
      <div className="space-y-3">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <ShieldCheck className="h-3 w-3 text-emerald-500" />
          <span>Capability grants are enforced by the cluster, not the prompt.</span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr>
                <th className="sticky left-0 bg-background p-1 text-left font-medium">Persona</th>
                {data.all_tools.map((tool) => (
                  <th
                    key={tool}
                    className="p-1 text-center align-bottom font-normal text-muted-foreground"
                  >
                    <span className="block [writing-mode:vertical-rl] rotate-180 whitespace-nowrap">
                      {shortTool(tool)}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.attendees.map((attendee) => (
                <tr key={attendee.agent_id} className="border-t">
                  <td className="sticky left-0 bg-background p-1">
                    {/* The whole security story is carried by icon colour and
                        hover text, neither of which a screen reader or a
                        keyboard-only operator can reach. State every cell in
                        text as well; the visual layout is unchanged. */}
                    <span className="sr-only">
                      {attendee.display_name}, profile {attendee.profile},{" "}
                      {attendee.can_execute_code
                        ? "may claim a code-execution sandbox"
                        : "refused code execution by RBAC"}
                      .
                    </span>
                    <Tooltip>
                      <TooltipTrigger render={<span className="cursor-help" />}>
                        <span className="font-medium">{attendee.title}</span>
                        <span className="ml-1 text-muted-foreground">{attendee.profile}</span>
                      </TooltipTrigger>
                      <TooltipContent>
                        <p className="font-medium">{attendee.display_name}</p>
                        <p className="max-w-xs text-xs text-muted-foreground">
                          {attendee.profile_description}
                        </p>
                        {attendee.can_execute_code ? (
                          <p className="mt-1 flex items-center gap-1 text-xs">
                            <Terminal className="h-3 w-3" /> May claim a code-execution sandbox
                          </p>
                        ) : (
                          <p className="mt-1 text-xs text-destructive">
                            Refused code execution by RBAC
                          </p>
                        )}
                      </TooltipContent>
                    </Tooltip>
                  </td>
                  {data.all_tools.map((tool) => {
                    const state = stateFor(attendee.agent_id, tool, attendee.granted_tools)
                    const { icon: Icon, className, label } = CELL[state]
                    return (
                      <td key={tool} className="p-1 text-center" data-state={state}>
                        <span className="sr-only">
                          {shortTool(tool)}: {label}
                        </span>
                        <Tooltip>
                          <TooltipTrigger render={<span className="inline-flex" />}>
                            <Icon className={`h-3.5 w-3.5 ${className}`} />
                          </TooltipTrigger>
                          <TooltipContent>
                            <p className="text-xs">
                              {shortTool(tool)}: {label}
                            </p>
                          </TooltipContent>
                        </Tooltip>
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="flex flex-wrap gap-3 text-[10px] text-muted-foreground">
          <span className="flex items-center gap-1">
            <Check className="h-3 w-3 text-emerald-500" /> used
          </span>
          <span className="flex items-center gap-1">
            <Circle className="h-3 w-3 text-muted-foreground/40" /> granted
          </span>
          <span className="flex items-center gap-1">
            <Ban className="h-3 w-3 text-destructive" /> refused by the API server
          </span>
        </div>
      </div>
    </TooltipProvider>
  )
}
