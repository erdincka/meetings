import "@testing-library/jest-dom/vitest"

import { afterEach, vi } from "vitest"
import { cleanup } from "@testing-library/react"

// jsdom has no ResizeObserver or matchMedia, which several Radix/Base UI
// primitives touch on mount. Stubbing them here keeps every component test from
// opening with the same four lines of unrelated setup.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal("ResizeObserver", ResizeObserverStub)

// Node 22 exposes a global `localStorage` that throws without --localstorage-file,
// and it shadows the one jsdom would otherwise provide. An in-memory Storage is
// enough for what is asserted here: that a token is written, read and cleared.
if (!window.localStorage || typeof window.localStorage.clear !== "function") {
  const entries = new Map<string, string>()
  const storage: Storage = {
    get length() {
      return entries.size
    },
    clear: () => entries.clear(),
    getItem: (key: string) => entries.get(key) ?? null,
    key: (index: number) => Array.from(entries.keys())[index] ?? null,
    removeItem: (key: string) => void entries.delete(key),
    setItem: (key: string, value: string) => void entries.set(key, String(value)),
  }
  Object.defineProperty(window, "localStorage", { value: storage, writable: true })
}

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
})

afterEach(() => {
  cleanup()
  window.localStorage.clear()
})
