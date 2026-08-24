# Learning path

The phases in the [README roadmap](../README.md#roadmap) read as a
changelog. Read in order, they're also a curriculum: each stage answers one
question a production agentic platform has to answer before the next
question becomes worth asking. This document is that reading, aimed at
someone learning the pattern — Kubernetes-native isolation for untrusted
agent execution — rather than someone just trying to run the demo.

Each stage names the question it answers, where to look, the thing that
would surprise you if you skipped it, and a command that proves the claim
rather than asserting it. That last part is a project-wide habit worth
learning on its own: every claim below has a command next to it, because a
control that "should" work and one that does are indistinguishable from the
outside until provoked.

## 0. Is the kernel boundary actually there?

**Question:** before writing a line of agent code — can untrusted code
really be contained, or does the isolation only look real?

**Look at:** [`sandbox-security-model.md`](sandbox-security-model.md),
`deploy/bootstrap/smoke-gvisor.yaml`.

**Nugget:** a misconfigured `RuntimeClass` handler does not fail loudly. On
several setups containerd silently falls back to `runc`, producing a green,
`Ready` pod with zero isolation — a fake security story indistinguishable
from a real one by anything that only checks pod status. The gate greps
`/proc/version` for gVisor's distinctive sentry string (`4.19.0-gvisor`)
instead, and asserting on that string rather than on readiness is the whole
lesson of this stage. The same failure mode recurred at a different layer in
Phase 3: `kindnetd` accepted `NetworkPolicy` objects and enforced none of
them.

**Try it:**

```bash
make smoke-gvisor
kubectl -n meetings-sandboxes exec <a persona pod> -- cat /proc/version
```

## 1. Is the configuration trustworthy enough to build on?

**Question:** before any feature work — is there a plaintext credential
anywhere, and does the schema drift silently from the code that reads it?

**Look at:** `backend/app/core/config.py`, `backend/alembic/`.

**Nugget:** the project's original form kept credentials in a plaintext
`config.json`. Replacing it with environment-only config and a
`system_settings` table — where a request to set a credential through the
settings API is a `422`, not a soft warning — happened *before* a single
persona feature shipped in this rebuild. Hygiene precedes features when the
eventual claim is "production-grade," not after; retrofitting it under a
working feature set is a much larger diff.

**Try it:**

```bash
make migrate-check   # fails if the ORM has drifted from the migrations
```

## 2. Can the agent's own reasoning run somewhere untrusted?

**Question:** the kernel boundary exists — now can a real ReAct loop
actually execute inside it, remotely, without becoming a distributed-systems
project?

**Look at:** `backend/app/orchestration/agents.py` (its docstring is the
whole design rationale in five sentences), `sandbox/runtime/runtime/agent.py`.

**Nugget:** the LangGraph graph never leaves the backend. Sandboxes are turn
executors, not graph participants — the entire ReAct loop for one turn runs
remotely and returns one result, rather than RPC-ing each tool call back to
the backend individually. The design note worth remembering: distributed
checkpointing across sandboxes was named and explicitly scoped *out* as a
research project, not a demo requirement. Knowing what not to distribute is
as load-bearing a decision as the isolation itself.

**Try it:**

```bash
kubectl -n meetings-sandboxes get pods -w   # watch a pod get claimed as a turn starts
```

## 3. Who decides what the agent may do — the prompt, or the cluster?

**Question:** an agent that can execute arbitrary Python is dangerous by
default. What actually stops it from reaching things it shouldn't, and does
that control survive the agent being talked into trying?

**Look at:** [`architecture.md`](architecture.md#the-five-layer-enforcement-model),
[`verify-enforcement.md`](verify-enforcement.md),
`backend/app/orchestration/profiles.py`.

**Nugget:** five layers, and the one that matters most is the one that
doesn't ask the model anything: RBAC. `sandbox/runtime/runtime/tools/code_exec.py`
lets *any* persona construct the code-execution tool call — the apiserver
decides whether the resulting `SandboxClaim` succeeds, based on the
ServiceAccount already bound to that persona's pod. A jailbroken model
still gets a 403. The counter-intuitive design call in this layer: an
earlier version also blocked apiserver *network* access for low-privilege
profiles, which sounded stronger and was actually worse — the refusal
became a 60-second connection timeout, indistinguishable from an outage. A
denial nobody can see is a control nobody can trust, so every profile keeps
apiserver reachability and the refusal comes back as a fast, legible 403.

**Try it:**

```bash
kubectl auth can-i create sandboxclaims.extensions.agents.x-k8s.io \
  --as=system:serviceaccount:meetings-sandboxes:persona-counsel -n meetings-exec   # no
kubectl auth can-i create sandboxclaims.extensions.agents.x-k8s.io \
  --as=system:serviceaccount:meetings-sandboxes:persona-quant -n meetings-exec     # yes
```

## 4. Does it survive contact with a real model?

**Question:** the architecture is sound on paper — what breaks when a live,
imperfect model actually drives it for an hour?

**Look at:** the eight fix commits in
[PR #7](https://github.com/erdincka/meetings/pull/7) — `4e8fb88`,
`100c92c`, `3148e0a`, `325f0d2`, `d7a6aab`, `0799db5`.

**Nugget:** this is the stage most tutorials skip, and where most of the
real learning happened. An async httpx client with a *synchronous* event
hook silently swallowed every tool call — `await`-ing `None` — and looked
like random turn failures until tracebacks were rendered instead of logged
as the literal string `true`. A reasoning-capable model burned its entire
token budget on internal thought and never answered as chair. An unclosed
`<thought>` tag leaked a General Counsel's private reasoning straight into
the public transcript. None of these are exotic bugs; all of them are what
"the demo works against a stub" hides. Isolation makes failures quieter, not
louder — which means observability into *why* a turn failed matters more
once execution moves off-process, not less.

**Try it:** run an actual meeting end to end and watch the runtime log for
a turn, per [`demo-script.md`](demo-script.md#3-the-agents-reach-for-tools-3-min).

## 5. Can you see what's happening across the boundary you just built?

**Question:** a turn now crosses three trust boundaries. When one is slow,
denied, or broken, can you tell which — and tell the difference between
those three outcomes — from the outside?

**Look at:** `backend/app/core/telemetry.py`,
[`docs/dashboards/agentic-meetings.json`](dashboards/agentic-meetings.json).

**Nugget:** a single `traceparent` header is propagated over the sandbox
RPC, and propagated *again* when a persona pod claims an exec sandbox — so
one meeting turn renders as one trace spanning backend, persona sandbox and
exec sandbox, the same three boundaries named in stage 2 and enforced in
stage 3. This is the same legibility argument as the RBAC-over-NetworkPolicy
call in stage 3, generalized: a system where "slow," "denied" and "broken"
look identical from outside is not one you can safely grant more autonomy
to, no matter how good its isolation is.

**Try it:**

```bash
make observability        # Prometheus + Grafana + Tempo, ~1.5GB
make deploy-observed
```

## 6. Would you actually operate this?

**Question:** everything above is real isolation and a real audit trail —
is it a platform you'd hand to someone else to run?

**Status:** not started. This is the gap between "a demo you can
screenshot" and "a platform you'd trust with production traffic," and it's
mostly supply-chain trust and whole-system confidence, not new isolation
primitives:

- **Integration/e2e depth** — the existing test suite (11 backend files, 4
  sandbox-runtime files, 0 frontend files) proves units work in isolation;
  nothing currently proves a meeting survives a pod eviction mid-turn or a
  warm pool running dry under load.
- **Operator authentication** — the UI and API currently assume a trusted
  operator. Nothing distinguishes "the person allowed to edit personas" from
  "the person allowed to read a transcript."
- **Signed, provenance-attested images** — `make images` tags by content
  digest today, which solves cache-staleness; it doesn't yet answer "did
  this image come from this repository's CI," which is the actual
  production question.

## Where a reader goes next

Someone who has walked all seven stages has effectively rebuilt the
argument for Kubernetes-native agent isolation from the ground up: kernel
boundary → remote execution → cluster-enforced privilege → what breaks
under a real model → observability across the boundary → what's left before
you'd operate it. The natural next reading is
[`architecture.md`](architecture.md) as a reference rather than a narrative,
and the roadmap's Phase 6 as the open problem.
