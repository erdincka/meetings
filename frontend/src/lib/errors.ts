import { AxiosError } from "axios"

/**
 * Extract a human-readable message from an unknown thrown value.
 *
 * Replaces the `catch (err: any)` + `err.response?.data?.detail || ...` chain
 * that was repeated across components. FastAPI returns `detail` for
 * HTTPException and `message` for the APIResponse envelope, so both are
 * checked before falling back to the Error message.
 */
export function errorMessage(err: unknown, fallback = "Something went wrong"): string {
  if (err instanceof AxiosError) {
    const body = err.response?.data as { detail?: string; message?: string } | undefined
    return body?.detail ?? body?.message ?? err.message ?? fallback
  }
  if (err instanceof Error) {
    return err.message || fallback
  }
  if (typeof err === "string" && err) {
    return err
  }
  return fallback
}
