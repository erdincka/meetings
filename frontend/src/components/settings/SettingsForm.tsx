"use client"

import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Loader2, RotateCcw, Save } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { Badge } from "@/components/ui/badge"
import { apiClient } from "@/lib/api-client"
import type { PromptMetadata, SystemSettings, SystemSettingsUpdate } from "@/lib/types"

/**
 * Operator-tunable settings.
 *
 * Scope is deliberately narrow: endpoints and API keys are environment-supplied
 * and no longer editable here (the API rejects them with a 422). What remains
 * is what an operator may legitimately change at runtime -- prompts, limits and
 * sampling temperatures -- all persisted in the `system_settings` table.
 *
 * These fields were previously editable but read by nothing: agents.py
 * hardcoded temperature 0.7, supervisor.py 0.1, and the retrieval tool a limit
 * of 3. They are wired through for real now.
 */

interface NumericField {
  key: keyof SystemSettingsUpdate
  label: string
  hint: string
  min: number
  max: number
  step: number
}

const NUMERIC_FIELDS: NumericField[] = [
  {
    key: "inference_temperature",
    label: "Agent temperature",
    hint: "Higher values make participants more varied and argumentative.",
    min: 0,
    max: 2,
    step: 0.05,
  },
  {
    key: "supervisor_temperature",
    label: "Supervisor temperature",
    hint: "Keep low. The supervisor emits structured output and small models drift badly above ~0.3.",
    min: 0,
    max: 2,
    step: 0.05,
  },
  {
    key: "retrieval_limits_per_agent",
    label: "Retrieval results per lookup",
    hint: "Document excerpts returned to an agent per search.",
    min: 1,
    max: 20,
    step: 1,
  },
  {
    key: "max_evidence_per_message",
    label: "Max evidence per message",
    hint: "Upper bound on citations a participant may attach to one contribution.",
    min: 1,
    max: 50,
    step: 1,
  },
  {
    key: "default_turn_limit",
    label: "Default turn limit",
    hint: "Applied when a meeting is created without an explicit limit.",
    min: 1,
    max: 500,
    step: 1,
  },
]

export default function SettingsForm() {
  const queryClient = useQueryClient()
  // Holds only the fields the operator has actually changed. Deriving the
  // displayed value as `edits[key] ?? settings[key]` avoids copying server
  // state into local state in an effect, and means the PATCH body is a genuine
  // partial update rather than a full round-trip of every field.
  const [edits, setEdits] = useState<SystemSettingsUpdate>({})

  const { data: settings, isLoading } = useQuery<SystemSettings>({
    queryKey: ["settings"],
    queryFn: () => apiClient.get<SystemSettings>("/settings"),
  })

  const { data: promptMetadata } = useQuery<PromptMetadata>({
    queryKey: ["prompt_metadata"],
    queryFn: () => apiClient.get<PromptMetadata>("/settings/prompts/metadata"),
  })

  const save = useMutation({
    mutationFn: (payload: SystemSettingsUpdate) =>
      apiClient.patch<SystemSettings>("/settings", payload),
    onSuccess: () => {
      toast.success("Settings saved")
      setEdits({})
      void queryClient.invalidateQueries({ queryKey: ["settings"] })
    },
    onError: (error: Error) => {
      toast.error("Could not save settings", { description: error.message })
    },
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center p-12 text-muted-foreground">
        <Loader2 className="mr-3 h-5 w-5 animate-spin" /> Loading settings…
      </div>
    )
  }

  const setField = (key: keyof SystemSettingsUpdate, value: unknown) =>
    setEdits((prev) => ({ ...prev, [key]: value }))

  /** Displayed value: the operator's edit if present, otherwise what is stored. */
  const valueOf = <K extends keyof SystemSettingsUpdate>(key: K): SystemSettingsUpdate[K] =>
    key in edits ? edits[key] : (settings?.[key as keyof SystemSettings] as SystemSettingsUpdate[K])

  return (
    <div className="space-y-6 pb-10">
      <Tabs defaultValue="behaviour">
        <TabsList className="mb-6">
          <TabsTrigger value="behaviour">Behaviour</TabsTrigger>
          <TabsTrigger value="prompts">Prompts</TabsTrigger>
        </TabsList>

        <TabsContent value="behaviour">
          <Card>
            <CardHeader>
              <CardTitle>Simulation behaviour</CardTitle>
              <CardDescription>
                Applied to every meeting started after saving.
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-6 sm:grid-cols-2">
              {NUMERIC_FIELDS.map((field) => (
                <div key={String(field.key)} className="space-y-2">
                  <Label htmlFor={String(field.key)}>{field.label}</Label>
                  <Input
                    id={String(field.key)}
                    type="number"
                    min={field.min}
                    max={field.max}
                    step={field.step}
                    value={String(valueOf(field.key) ?? "")}
                    onChange={(e) =>
                      setField(field.key, e.target.value === "" ? undefined : Number(e.target.value))
                    }
                  />
                  <p className="text-xs text-muted-foreground">{field.hint}</p>
                </div>
              ))}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="prompts" className="space-y-6">
          {promptMetadata
            ? Object.entries(promptMetadata).map(([key, meta]) => {
                const field = key as keyof SystemSettingsUpdate
                const value = (valueOf(field) as string | null | undefined) ?? ""
                return (
                  <Card key={key}>
                    <CardHeader>
                      <CardTitle className="text-base">{meta.title}</CardTitle>
                      <CardDescription>{meta.description}</CardDescription>
                      <div className="flex flex-wrap gap-1 pt-2">
                        {meta.placeholders.map((p) => (
                          <Badge key={p} variant="secondary" className="font-mono text-[10px]">
                            {p}
                          </Badge>
                        ))}
                      </div>
                    </CardHeader>
                    <CardContent className="space-y-3">
                      <Textarea
                        rows={12}
                        className="font-mono text-xs"
                        placeholder={meta.default}
                        value={value}
                        onChange={(e) => setField(field, e.target.value)}
                      />
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setField(field, meta.default)
                          toast.info(`Reset ${meta.title} to the default`)
                        }}
                      >
                        <RotateCcw className="mr-2 h-3 w-3" />
                        Reset to default
                      </Button>
                    </CardContent>
                  </Card>
                )
              })
            : null}
        </TabsContent>
      </Tabs>

      <div className="flex justify-end">
        <Button onClick={() => save.mutate(edits)} disabled={save.isPending || Object.keys(edits).length === 0}>
          {save.isPending ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <Save className="mr-2 h-4 w-4" />
          )}
          Save settings
        </Button>
      </div>
    </div>
  )
}
