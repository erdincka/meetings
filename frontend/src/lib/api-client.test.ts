import { describe, expect, it, beforeEach, vi } from "vitest"
import MockAdapter from "axios-mock-adapter"

import { apiClient } from "@/lib/api-client"
import { readToken, storeToken, subscribeToToken } from "@/lib/auth"

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
    warning: vi.fn(),
    success: vi.fn(),
  },
}))

// The client owns its axios instance, so the adapter is attached to the same
// one the app uses rather than to a copy built for the test.
const axiosInstance = (apiClient as unknown as { client: never }).client
const mock = new MockAdapter(axiosInstance)

beforeEach(() => {
  mock.reset()
  window.localStorage.clear()
})

describe("the response envelope", () => {
  it("unwraps a success envelope to its data", async () => {
    mock.onGet("/roles").reply(200, { status: "success", data: [{ id: "1" }] })
    await expect(apiClient.get("/roles")).resolves.toEqual([{ id: "1" }])
  })

  it("treats an error envelope as a failure even on HTTP 200", async () => {
    // Several routes answer 200 with status:"error". Returning data blindly
    // made those resolve as successful mutations carrying `undefined`, and the
    // UI showed a success toast for a write that never happened.
    mock.onPost("/roles").reply(200, { status: "error", message: "Role not found" })
    await expect(apiClient.post("/roles", {})).rejects.toThrow("Role not found")
  })

  it("applies the same rule to every verb, not only GET", async () => {
    for (const [verb, call] of [
      ["put", () => apiClient.put("/x", {})],
      ["patch", () => apiClient.patch("/x", {})],
      ["delete", () => apiClient.delete("/x")],
    ] as const) {
      mock.reset()
      mock.onAny("/x").reply(200, { status: "error", message: `${verb} failed` })
      await expect(call()).rejects.toThrow(`${verb} failed`)
    }
  })
})

describe("the operator token", () => {
  it("is attached to every request once stored", async () => {
    storeToken("op-secret")
    mock.onGet("/roles").reply(200, { status: "success", data: [] })

    await apiClient.get("/roles")

    expect(mock.history.get[0].headers?.Authorization).toBe("Bearer op-secret")
  })

  it("is absent when none is stored", async () => {
    mock.onGet("/auth/config").reply(200, { status: "success", data: {} })
    await apiClient.get("/auth/config")
    expect(mock.history.get[0].headers?.Authorization).toBeUndefined()
  })

  it("is read per request, so signing in mid-session takes effect", async () => {
    mock.onGet("/roles").reply(200, { status: "success", data: [] })

    await apiClient.get("/roles")
    storeToken("issued-later")
    await apiClient.get("/roles")

    expect(mock.history.get[0].headers?.Authorization).toBeUndefined()
    expect(mock.history.get[1].headers?.Authorization).toBe("Bearer issued-later")
  })
})

describe("a rejected token", () => {
  it("is discarded, so the guard re-prompts instead of looping on 401", async () => {
    storeToken("rotated-away")
    mock.onGet("/roles").reply(401, { detail: "Operator token rejected" })

    await expect(apiClient.get("/roles")).rejects.toBeDefined()

    expect(readToken()).toBeNull()
  })

  it("notifies subscribers, so the guard re-prompts without polling", async () => {
    storeToken("rotated-away")
    mock.onGet("/roles").reply(401, { detail: "no" })
    const listener = vi.fn()
    const unsubscribe = subscribeToToken(listener)

    await expect(apiClient.get("/roles")).rejects.toBeDefined()

    expect(listener).toHaveBeenCalled()
    unsubscribe()
  })

  it("keeps the token on a 403, which is a permission answer, not a bad token", async () => {
    // A viewer hitting a mutating route is correctly signed in. Clearing the
    // token there would sign them out every time they clicked the wrong button.
    storeToken("viewer-token")
    mock.onPost("/roles").reply(403, { detail: "Role 'operator' is required" })

    await expect(apiClient.post("/roles", {})).rejects.toBeDefined()

    expect(readToken()).toBe("viewer-token")
  })
})
