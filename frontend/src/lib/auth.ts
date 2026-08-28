/**
 * Operator token handling.
 *
 * The token is a shared secret an administrator issues out of band and pastes
 * in once. There is no login form talking to a user table behind this, because
 * there is no user table: the tokens live in a Kubernetes Secret, so the
 * browser's only jobs are to hold one and to attach it.
 *
 * Storage is `localStorage`, deliberately and with the trade-off stated: it
 * survives a reload, which is what makes the UI usable, and it is readable by
 * any script running on this origin, which is what makes it unsuitable for a
 * multi-tenant deployment. The role split is the mitigation that matters here —
 * a leaked viewer token reads a transcript; it cannot change what an agent is
 * permitted to do.
 */

export type OperatorRole = "viewer" | "operator"

const STORAGE_KEY = "meetings.operator-token"

// Subscribers, so React can treat the token as what it is -- an external store
// the app reads rather than state the app owns. That is also why the token is
// not a useState + useEffect pair: reading storage in an effect means the first
// render is always wrong, and every consumer has to cope with a null that only
// means "not read yet".
const listeners = new Set<() => void>()

function announce(): void {
  for (const listener of listeners) listener()
}

/** Subscribe to token changes, including those made in another browser tab. */
export function subscribeToToken(onChange: () => void): () => void {
  listeners.add(onChange)
  if (typeof window !== "undefined") {
    window.addEventListener("storage", onChange)
  }
  return () => {
    listeners.delete(onChange)
    if (typeof window !== "undefined") {
      window.removeEventListener("storage", onChange)
    }
  }
}

/** Read the stored token, or null. Safe to call during SSR. */
export function readToken(): string | null {
  if (typeof window === "undefined") return null
  try {
    return window.localStorage.getItem(STORAGE_KEY)
  } catch {
    // Private browsing and hardened profiles throw rather than returning null.
    return null
  }
}

/** The server render has no storage, and must not guess at one. */
export function readTokenOnServer(): null {
  return null
}

export function storeToken(token: string): void {
  if (typeof window === "undefined") return
  try {
    window.localStorage.setItem(STORAGE_KEY, token)
  } catch {
    /* nothing useful to do; the session simply will not persist */
  }
  announce()
}

export function clearToken(): void {
  if (typeof window === "undefined") return
  try {
    window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* as above */
  }
  announce()
}

export interface WhoAmI {
  role: OperatorRole
  can_mutate: boolean
}

export interface AuthConfig {
  auth_required: boolean
  scheme: string
}

/**
 * Subprotocols for an authenticated WebSocket handshake.
 *
 * A browser cannot set headers on `new WebSocket(...)`; the subprotocol list is
 * the only field the caller controls. Unlike a query string it does not land in
 * proxy access logs or browser history.
 */
export function websocketProtocols(token: string | null): string[] | undefined {
  return token ? ["bearer", token] : undefined
}
