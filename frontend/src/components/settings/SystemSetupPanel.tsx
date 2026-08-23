"use client"

import { useMutation, useQueryClient } from "@tanstack/react-query"
import { AlertCircle, CheckCircle2, Copy, Loader2, XCircle } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { apiClient } from "@/lib/api-client"
import type { SystemStatus } from "@/lib/types"

/**
 * Setup diagnostics.
 *
 * This panel used to collect a database URI and two API keys and POST them to
 * the backend, which wrote them to a plaintext file on a PVC. Credentials now
 * come from the `meetings-runtime` Secret, so the panel's job is to report what
 * the operator has configured, show the exact command to fix what is missing,
 * and trigger migrations plus seeding.
 */

function Check({ ok, label, detail }: { ok: boolean; label: string; detail?: string }) {
  return (
    <div className="flex items-start gap-3 py-2">
      {ok ? (
        <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
      ) : (
        <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
      )}
      <div className="min-w-0">
        <p className="text-sm font-medium">{label}</p>
        {detail ? (
          <p className="break-words text-xs text-muted-foreground">{detail}</p>
        ) : null}
      </div>
    </div>
  )
}

export default function SystemSetupPanel({
  status,
  onDone,
}: {
  status: SystemStatus | undefined
  onDone?: () => void
}) {
  const queryClient = useQueryClient()

  const setup = useMutation({
    mutationFn: () => apiClient.post<unknown>("/system/setup", {}),
    onSuccess: () => {
      toast.success("Setup started", {
        description: "Applying migrations and seeding reference data.",
      })
      void queryClient.invalidateQueries({ queryKey: ["system_status"] })
      onDone?.()
    },
    onError: (error: Error) => {
      toast.error("Could not start setup", { description: error.message })
    },
  })

  const dbNeedsSetup = !status?.db_configured
  const running = setup.isPending || status?.last_op?.status === "pending"

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Configuration</CardTitle>
          <CardDescription>
            Supplied by the environment (the <code>meetings-runtime</code> Secret and{" "}
            <code>meetings-config</code> ConfigMap). These values are not editable from
            the browser by design — the application never handles credentials it could
            write to disk.
          </CardDescription>
        </CardHeader>
        <CardContent className="divide-y">
          <Check
            ok={!!status?.db_configured}
            label="Database"
            detail={
              status?.db_configured
                ? "Connected, migrated and seeded."
                : status?.reasons.find((r) => r.startsWith("Database")) ??
                  "Not reachable."
            }
          />
          <Check
            ok={!!status?.inference_verified}
            label="Inference endpoint"
            detail={status?.inference_status}
          />
          <Check
            ok={!!status?.embedding_verified}
            label="Embedding endpoint"
            detail={status?.embedding_status}
          />
        </CardContent>
      </Card>

      {status?.remediation ? (
        <Card className="border-amber-500/30">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <AlertCircle className="h-4 w-4 text-amber-500" />
              Missing configuration
            </CardTitle>
            <CardDescription>
              Create or update the Secret, then restart the backend deployment.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <pre className="overflow-x-auto rounded-md bg-muted p-3 text-xs leading-relaxed">
              <code>{status.remediation}</code>
            </pre>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                void navigator.clipboard.writeText(status.remediation ?? "")
                toast.success("Copied")
              }}
            >
              <Copy className="mr-2 h-3 w-3" />
              Copy command
            </Button>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Database setup</CardTitle>
          <CardDescription>
            Applies Alembic migrations and loads reference personas, documents and
            meeting templates. Safe to re-run: seeding skips anything already present.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {status?.last_op?.status === "error" ? (
            <p className="text-sm text-destructive">{status.last_op.message}</p>
          ) : null}
          <Button onClick={() => setup.mutate()} disabled={running}>
            {running ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
            {running ? "Running…" : dbNeedsSetup ? "Run setup" : "Re-run setup"}
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
