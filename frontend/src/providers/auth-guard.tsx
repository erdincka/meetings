"use client"

import {
  createContext,
  useCallback,
  useContext,
  useSyncExternalStore,
  useState,
  type ReactNode,
} from "react"
import { useQuery } from "@tanstack/react-query"
import { KeyRound, Loader2, ShieldCheck } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { apiClient } from "@/lib/api-client"
import {
  clearToken,
  readToken,
  readTokenOnServer,
  storeToken,
  subscribeToToken,
  type AuthConfig,
  type WhoAmI,
} from "@/lib/auth"

interface OperatorSession {
  /** null while authentication is disabled on the backend. */
  role: WhoAmI["role"] | null
  /** True when this session may change what agents are permitted to do. */
  canMutate: boolean
  authRequired: boolean
  signOut: () => void
}

const SessionContext = createContext<OperatorSession>({
  role: null,
  canMutate: true,
  authRequired: false,
  signOut: () => {},
})

/**
 * The current operator session.
 *
 * `canMutate` is the one to branch on. It is true when authentication is off,
 * because a deployment that has switched it off has said every caller is an
 * operator; it is the backend, not this hook, that enforces either answer.
 */
export function useOperatorSession(): OperatorSession {
  return useContext(SessionContext)
}

/**
 * Gates the app on a valid operator token.
 *
 * Sits outside SetupGuard: whether the backend is configured is itself
 * information, and answering it should require a token wherever one is
 * required at all.
 */
export default function AuthGuard({ children }: { children: ReactNode }) {
  // The token is an external store, not component state: it lives in
  // localStorage, it can change in another tab, and the API client clears it
  // when the backend rejects it. useSyncExternalStore covers all three without
  // an effect that would make the first render wrong by construction.
  const token = useSyncExternalStore(subscribeToToken, readToken, readTokenOnServer)

  const { data: config, isLoading: configLoading } = useQuery<AuthConfig>({
    queryKey: ["auth_config"],
    queryFn: () => apiClient.get<AuthConfig>("/auth/config"),
    staleTime: Infinity,
    retry: 1,
  })

  const {
    data: session,
    isLoading: sessionLoading,
    isError,
  } = useQuery<WhoAmI>({
    queryKey: ["whoami", token],
    queryFn: () => apiClient.get<WhoAmI>("/auth/whoami"),
    enabled: Boolean(config?.auth_required && token),
    retry: false,
    staleTime: 5 * 60 * 1000,
  })

  const signOut = useCallback(() => clearToken(), [])
  const onSubmit = useCallback((value: string) => storeToken(value), [])

  if (configLoading) {
    return <FullScreenSpinner label="Contacting backend…" />
  }

  if (!config?.auth_required) {
    return (
      <SessionContext.Provider
        value={{ role: null, canMutate: true, authRequired: false, signOut }}
      >
        {children}
      </SessionContext.Provider>
    )
  }

  if (!token || isError) {
    return <SignIn onSubmit={onSubmit} rejected={Boolean(token && isError)} />
  }

  if (sessionLoading || !session) {
    return <FullScreenSpinner label="Checking your token…" />
  }

  return (
    <SessionContext.Provider
      value={{
        role: session.role,
        canMutate: session.can_mutate,
        authRequired: true,
        signOut,
      }}
    >
      {session.can_mutate ? null : <ReadOnlyBanner />}
      {children}
    </SessionContext.Provider>
  )
}

function FullScreenSpinner({ label }: { label: string }) {
  return (
    <div className="fixed inset-0 z-[9999] flex flex-col items-center justify-center gap-4 bg-background">
      <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      <p className="text-sm text-muted-foreground">{label}</p>
    </div>
  )
}

/**
 * Shown to a viewer, permanently.
 *
 * A viewer who does not know they are a viewer reads a greyed-out button as a
 * broken UI. Naming the role turns a puzzling failure into an expected one.
 */
function ReadOnlyBanner() {
  return (
    <div
      role="status"
      className="fixed bottom-4 left-1/2 z-50 -translate-x-1/2 rounded-full border bg-background/95 px-4 py-1.5 text-xs text-muted-foreground shadow-sm"
    >
      Read-only session — an operator token is needed to change personas, settings or meetings.
    </div>
  )
}

function SignIn({
  onSubmit,
  rejected,
}: {
  onSubmit: (token: string) => void
  rejected: boolean
}) {
  const [value, setValue] = useState("")

  return (
    <div className="fixed inset-0 z-[9999] overflow-auto bg-background">
      <div className="container mx-auto max-w-md px-6 py-24">
        <div className="mb-8 space-y-3">
          <div className="inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium text-muted-foreground">
            <ShieldCheck className="h-3 w-3" />
            Operator token required
          </div>
          <h1 className="text-3xl font-semibold tracking-tight">Agentic Meetings</h1>
          <p className="text-sm text-muted-foreground">
            This deployment requires a token. An operator token can change what agents are
            permitted to do; a viewer token can only read.
          </p>
        </div>

        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            if (value.trim()) onSubmit(value.trim())
          }}
        >
          <div className="space-y-2">
            <Label htmlFor="operator-token">Token</Label>
            <Input
              id="operator-token"
              type="password"
              autoComplete="off"
              autoFocus
              value={value}
              onChange={(event) => setValue(event.target.value)}
              placeholder="paste the token"
            />
          </div>

          {rejected ? (
            <p className="text-sm text-destructive">
              That token was rejected. It may have been rotated.
            </p>
          ) : null}

          <Button type="submit" className="w-full" disabled={!value.trim()}>
            <KeyRound className="mr-2 h-4 w-4" />
            Sign in
          </Button>
        </form>

        <p className="mt-8 text-xs text-muted-foreground">
          Tokens come from the <code>meetings-runtime</code> Secret. See{" "}
          <code>docs/operations.md</code> for how to read or rotate one.
        </p>
      </div>
    </div>
  )
}
