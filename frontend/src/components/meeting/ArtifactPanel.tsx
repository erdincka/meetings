"use client"

import { useQuery } from "@tanstack/react-query"
import { CheckSquare, FileText, Image as ImageIcon } from "lucide-react"
import ReactMarkdown from "react-markdown"

import { Badge } from "@/components/ui/badge"
import { apiClient } from "@/lib/api-client"
import type { MeetingArtifacts } from "@/lib/types"
import type { Role } from "@/lib/types"

/**
 * What the meeting produced, rather than what it said.
 *
 * Charts render inline: an analysis that returns a PNG the reader has to
 * download is an analysis nobody looks at. Images arrive base64-encoded in the
 * artifact body, which keeps sandboxes free of any shared filesystem.
 */
export default function ArtifactPanel({
  meetingId,
  attendees,
}: {
  meetingId: string
  attendees: Record<string, Role>
}) {
  const { data } = useQuery<MeetingArtifacts>({
    queryKey: ["meeting_artifacts", meetingId],
    queryFn: () => apiClient.get<MeetingArtifacts>(`/meetings/${meetingId}/artifacts`),
    // Artifacts appear mid-meeting, so keep it fresh while one is running.
    refetchInterval: 15000,
  })

  const artifacts = data?.artifacts ?? []
  const actions = data?.action_items ?? []

  if (artifacts.length === 0 && actions.length === 0) {
    return (
      <p className="p-4 text-xs text-muted-foreground">
        Nothing produced yet. Charts, drafts and commitments appear here as
        participants create them.
      </p>
    )
  }

  const author = (agentId: string | null) =>
    agentId ? (attendees[agentId]?.display_name ?? "Unknown") : "Unattributed"

  return (
    <div className="space-y-6 p-4">
      {actions.length > 0 ? (
        <section className="space-y-2">
          <h4 className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-muted-foreground">
            <CheckSquare className="h-3 w-3" /> Action items
          </h4>
          <ul className="space-y-1.5">
            {actions.map((item) => (
              <li key={item.id} className="rounded-md border p-2 text-xs">
                <p>{item.text}</p>
                <p className="mt-1 text-[10px] text-muted-foreground">
                  raised by {author(item.raised_by_agent_id)}
                  {item.due ? ` · due ${item.due}` : ""}
                </p>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {artifacts.length > 0 ? (
        <section className="space-y-3">
          <h4 className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-muted-foreground">
            <FileText className="h-3 w-3" /> Artifacts
          </h4>
          {artifacts.map((artifact) => {
            const isImage = artifact.mime_type.startsWith("image/")
            return (
              <article key={artifact.id} className="space-y-2 rounded-md border p-3">
                <header className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-1.5 text-xs font-medium">
                    {isImage ? <ImageIcon className="h-3 w-3" /> : <FileText className="h-3 w-3" />}
                    {artifact.title}
                  </span>
                  <Badge variant="secondary" className="text-[9px]">
                    {author(artifact.agent_id)}
                  </Badge>
                </header>

                {isImage ? (
                  // eslint-disable-next-line @next/next/no-img-element -- a data
                  // URI from the meeting's own database; next/image would need a
                  // loader and buys nothing here.
                  <img
                    src={`data:${artifact.mime_type};base64,${artifact.body}`}
                    alt={artifact.title}
                    className="w-full rounded border bg-white"
                  />
                ) : (
                  <div className="prose prose-sm dark:prose-invert max-w-none text-xs">
                    <ReactMarkdown>{artifact.body}</ReactMarkdown>
                  </div>
                )}
              </article>
            )
          })}
        </section>
      ) : null}
    </div>
  )
}
