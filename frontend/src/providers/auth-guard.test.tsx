import { describe, expect, it, vi, beforeEach } from "vitest"
import { act, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

import AuthGuard, { useOperatorSession } from "@/providers/auth-guard"
import { clearToken, readToken, storeToken } from "@/lib/auth"

const get = vi.fn()

vi.mock("@/lib/api-client", () => ({
  apiClient: {
    get: (url: string) => get(url),
  },
}))

function Protected() {
  const { role, canMutate } = useOperatorSession()
  return (
    <div>
      <p>protected content</p>
      <p data-testid="role">{role ?? "none"}</p>
      <p data-testid="can-mutate">{String(canMutate)}</p>
    </div>
  )
}

function renderGuard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AuthGuard>
        <Protected />
      </AuthGuard>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  get.mockReset()
  window.localStorage.clear()
})

describe("when the backend does not require a token", () => {
  it("renders the app without prompting", async () => {
    get.mockImplementation(async (url: string) => {
      if (url === "/auth/config") return { auth_required: false, scheme: "bearer" }
      throw new Error(`unexpected ${url}`)
    })

    renderGuard()

    expect(await screen.findByText("protected content")).toBeInTheDocument()
    // Auth off means the deployment has declared every caller an operator; the
    // UI must not then disable the controls the backend will happily accept.
    expect(screen.getByTestId("can-mutate")).toHaveTextContent("true")
  })

  it("never asks whoami, because there is no session to describe", async () => {
    get.mockResolvedValue({ auth_required: false, scheme: "bearer" })
    renderGuard()
    await screen.findByText("protected content")
    expect(get).not.toHaveBeenCalledWith("/auth/whoami")
  })
})

describe("when a token is required", () => {
  beforeEach(() => {
    get.mockImplementation(async (url: string) => {
      if (url === "/auth/config") return { auth_required: true, scheme: "bearer" }
      if (url === "/auth/whoami") {
        const token = readToken()
        if (token === "op-secret") return { role: "operator", can_mutate: true }
        if (token === "view-secret") return { role: "viewer", can_mutate: false }
        throw new Error("rejected")
      }
      throw new Error(`unexpected ${url}`)
    })
  })

  it("prompts, and does not render the app behind the prompt", async () => {
    renderGuard()

    expect(await screen.findByLabelText("Token")).toBeInTheDocument()
    expect(screen.queryByText("protected content")).not.toBeInTheDocument()
  })

  it("admits an operator and reports the role", async () => {
    const user = userEvent.setup()
    renderGuard()

    await user.type(await screen.findByLabelText("Token"), "op-secret")
    await user.click(screen.getByRole("button", { name: /sign in/i }))

    expect(await screen.findByText("protected content")).toBeInTheDocument()
    expect(screen.getByTestId("role")).toHaveTextContent("operator")
    expect(screen.getByTestId("can-mutate")).toHaveTextContent("true")
  })

  it("admits a viewer read-only, and says so on screen", async () => {
    const user = userEvent.setup()
    renderGuard()

    await user.type(await screen.findByLabelText("Token"), "view-secret")
    await user.click(screen.getByRole("button", { name: /sign in/i }))

    expect(await screen.findByText("protected content")).toBeInTheDocument()
    expect(screen.getByTestId("can-mutate")).toHaveTextContent("false")
    // A viewer who does not know they are a viewer reads a disabled button as
    // a broken UI rather than as a permission boundary.
    expect(screen.getByRole("status")).toHaveTextContent(/read-only session/i)
  })

  it("re-prompts and says why when a stored token has been rotated away", async () => {
    storeToken("stale-token")
    renderGuard()

    expect(await screen.findByText(/that token was rejected/i)).toBeInTheDocument()
  })

  it("re-prompts when the client reports the token was rejected mid-session", async () => {
    const user = userEvent.setup()
    renderGuard()

    await user.type(await screen.findByLabelText("Token"), "op-secret")
    await user.click(screen.getByRole("button", { name: /sign in/i }))
    await screen.findByText("protected content")

    // Exactly what the API client does on a 401.
    act(() => clearToken())

    await waitFor(() => expect(screen.getByLabelText("Token")).toBeInTheDocument())
  })
})
