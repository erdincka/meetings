"use client"

import { type ReactNode } from "react"
import { useQuery } from "@tanstack/react-query"
import { Loader2, ShieldCheck } from "lucide-react"

import SystemSetupPanel from "@/components/settings/SystemSetupPanel"
import { apiClient } from "@/lib/api-client"
import type { SystemStatus } from "@/lib/types"

/**
 * Blocks the app until the backend reports it can actually run a meeting.
 *
 * Polling was every 2-3s indefinitely, and each poll made the backend re-read
 * and re-parse a JSON config file from disk. /system/status is now an in-memory
 * read plus one COUNT, and polling stops once the system is ready.
 */
export default function SetupGuard({ children }: { children: ReactNode }) {
  const {
    data: status,
    isLoading,
    refetch,
  } = useQuery<SystemStatus>({
    queryKey: ["system_status"],
    queryFn: () => apiClient.get<SystemStatus>("/system/status"),
    retry: 2,
    refetchInterval: (query) => {
      const data = query.state.data
      if (data?.last_op?.status === "pending") return 2000
      // Stop polling entirely once configured.
      return data?.configured ? false : 10000
    },
  })

  if (isLoading) {
    return (
      <div className="fixed inset-0 z-[9999] flex flex-col items-center justify-center gap-4 bg-background">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
        <p className="text-sm text-muted-foreground">Contacting backend…</p>
      </div>
    )
  }

  if (status?.configured) {
    return <>{children}</>
  }

  return (
    <div className="fixed inset-0 z-[9999] overflow-auto bg-background">
      <div className="container mx-auto max-w-3xl px-6 py-16">
        <div className="mb-10 space-y-3">
          <div className="inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium text-muted-foreground">
            <ShieldCheck className="h-3 w-3" />
            Setup required
          </div>
          <h1 className="text-3xl font-semibold tracking-tight">Agentic Meetings</h1>
          <p className="max-w-xl text-muted-foreground">
            The backend is running but is not ready to hold a meeting yet. Everything it
            needs is listed below.
          </p>
        </div>

        <SystemSetupPanel status={status} onDone={() => void refetch()} />
      </div>
    </div>
  )
}
