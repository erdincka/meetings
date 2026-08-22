"use client"

import { Cpu, ShieldCheck } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { EventLogItem } from "@/store"
import type { Role } from "@/lib/types"

/**
 * Which attendee is running in which sandbox.
 *
 * The point of the project is that each participant's reasoning executes in its
 * own isolated pod, and that is invisible unless the UI says so. Sandbox names
 * come from the backend on each agent_spoke event.
 */
export default function SandboxStatus({
  eventLog,
  attendees,
}: {
  eventLog: EventLogItem[]
  attendees: Record<string, Role>
}) {
  // agent_id -> sandbox name, last one wins.
  const sandboxes = new Map<string, string>()
  for (const event of eventLog) {
    if (typeof event.sandbox === "string" && event.agent_id) {
      sandboxes.set(event.agent_id, event.sandbox)
    }
  }

  if (sandboxes.size === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        No sandboxes claimed yet. Each attendee gets one when the chair first
        calls on them.
      </p>
    )
  }

  return (
    <TooltipProvider>
      <div className="space-y-2">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <ShieldCheck className="h-3 w-3 text-emerald-500" />
          <span>
            {sandboxes.size} isolated {sandboxes.size === 1 ? "runtime" : "runtimes"}
          </span>
        </div>
        <ul className="space-y-1">
          {[...sandboxes.entries()].map(([agentId, sandbox]) => {
            const role = attendees[agentId]
            return (
              <li key={agentId} className="flex items-center justify-between gap-2 text-xs">
                <span className="truncate">{role?.display_name ?? agentId}</span>
                <Tooltip>
                  <TooltipTrigger
                    render={
                      <Badge
                        variant="secondary"
                        className="max-w-[11rem] shrink-0 font-mono"
                      />
                    }
                  >
                    <Cpu className="mr-1 h-3 w-3" />
                    <span className="truncate">{sandbox}</span>
                  </TooltipTrigger>
                  <TooltipContent>
                    <p className="font-mono text-xs">{sandbox}</p>
                    <p className="text-xs text-muted-foreground">
                      Claimed from the persona warm pool; runs under a sandboxed
                      kernel with its own network policy.
                    </p>
                  </TooltipContent>
                </Tooltip>
              </li>
            )
          })}
        </ul>
      </div>
    </TooltipProvider>
  )
}
