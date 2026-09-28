"use client"

import { useQuery } from "@tanstack/react-query"

import { apiClient } from "@/lib/api-client"
import type { Role } from "@/lib/types"

/**
 * What the cluster did with this meeting's sandboxes, in its own words.
 *
 * The capability matrix shows what each persona was allowed. This shows what
 * the platform did on its behalf -- adopted a warm sandbox, created a pod,
 * expired a claim -- as reported by the Agent Sandbox controller rather than
 * by this application.
 *
 * Live only, and labelled as such. Kubernetes keeps an Event for about an hour,
 * and the link from a sandbox to a persona is held in memory by the backend, so
 * this is a view of what is happening and not a record of what happened.
 */

type ClusterEvent = {
  time: string | null
  kind: string
  name: string
  type: string
  reason: string
  note: string | null
  agent_id: string | null
  profile: string | null
}

type SandboxEvents = { events: ClusterEvent[] }

export default function ClusterEvents({
  meetingId,
  attendees,
  live,
}: {
  meetingId: string
  attendees: Record<string, Role>
  live: boolean
}) {
  const { data, isError } = useQuery<SandboxEvents>({
    queryKey: ["meeting_sandbox_events", meetingId],
    queryFn: () => apiClient.get<SandboxEvents>(`/meetings/${meetingId}/sandbox-events`),
    enabled: live,
    refetchInterval: live ? 5000 : false,
    retry: false,
  })

  if (!live) {
    return (
      <p className="text-xs text-muted-foreground">
        Shown while a meeting runs. The cluster keeps these for about an hour.
      </p>
    )
  }
  if (isError) {
    return <p className="text-xs text-muted-foreground">Cluster events are unavailable.</p>
  }
  const events = data?.events ?? []
  if (events.length === 0) {
    return <p className="text-xs text-muted-foreground">Nothing reported yet.</p>
  }

  return (
    <ul className="space-y-1">
      {events.slice(-12).map((event, index) => (
        <li key={`${event.name}-${event.reason}-${index}`} className="text-xs">
          <span
            className={
              event.type === "Warning" ? "font-medium text-destructive" : "font-medium"
            }
          >
            {event.reason}
          </span>{" "}
          <span className="text-muted-foreground">
            {attendees[event.agent_id ?? ""]?.display_name ?? event.name}
            {event.profile ? ` · ${event.profile}` : ""}
          </span>
        </li>
      ))}
    </ul>
  )
}
