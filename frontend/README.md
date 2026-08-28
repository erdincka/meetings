# Frontend

Next.js App Router, React 19, Tailwind, TanStack Query for server state and
Zustand for the live meeting transcript.

```bash
npm ci
npm run dev      # http://localhost:3000, proxying /api/v1 to the backend
npm test         # vitest
npm run lint
npx tsc --noEmit
```

`NEXT_PUBLIC_API_URL` points at the backend; it defaults to `/api/v1`, which is
what the Gateway serves in a deployment.

## Shape

| | |
|---|---|
| `src/lib/api-client.ts` | The only place that talks HTTP. Attaches the operator token, unwraps the response envelope, and maps status codes to user-facing messages |
| `src/lib/auth.ts` | The operator token as an external store — it lives in `localStorage`, changes in other tabs, and is cleared by the API client on a 401 |
| `src/providers/auth-guard.tsx` | Gates the app on a valid token and publishes the session role |
| `src/providers/setup-guard.tsx` | Gates it again on the backend actually being able to run a meeting |
| `src/store/index.ts` | Folds the meeting WebSocket's event stream into what the transcript renders |
| `src/components/meeting/ToolAuditMatrix.tsx` | Granted, used and denied per persona — the screen the whole demo builds toward |

## Two things worth knowing before editing

**The response envelope is not optional.** Several backend routes answer HTTP
200 with `status: "error"`. `api-client` treats that as a failure for every
verb; bypassing it with a raw `axios` call resurrects a bug where a failed
mutation resolved successfully and the UI showed a success toast.

**The WebSocket carries its token as a subprotocol.** A browser cannot set an
`Authorization` header on `new WebSocket(...)`, and a query string would put the
token in every proxy access log between here and the pod. `websocketProtocols()`
in `src/lib/auth.ts` builds the list; the backend echoes it back on accept,
which the handshake requires.

## Capability state is stated in text, not only in colour

The audit matrix conveys granted/used/denied through icon colour. Every cell
also carries an `sr-only` sentence saying which it is, because a refusal that
only a sighted user hovering a tooltip can find is not an audit trail. Keep that
when adding cells.

## Tests

Vitest with jsdom. `src/test/setup.ts` provides the globals jsdom does not —
`ResizeObserver`, `matchMedia`, and an in-memory `localStorage`, which Node 22
otherwise shadows with a global that throws.

The tests target behaviour that would misrepresent something if it broke: the
role split, the response envelope, token handling on 401 versus 403, and the
audit matrix showing a denial as a denial.
