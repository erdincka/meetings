# Lessons learned

The reference documentation states what this system does and why it is built
that way. This document is the other thing worth writing down: what was learned
building it, including the parts that were wrong first.

It is separate on purpose. Reference documentation that keeps re-litigating its
own history is hard to read and ages badly, and a decision record that hides the
corrections is worth less than one that does not. Nothing here is needed to run
or extend the system — it is here because most of it generalises to any project
putting untrusted agent execution on Kubernetes.

---

## The controls that looked right and did nothing

This is the theme the project kept rediscovering, at four different layers. It
is the single most transferable lesson here, so it goes first.

**A misconfigured RuntimeClass handler does not fail loudly.** On several
container runtimes, containerd silently falls back to `runc`. The pod is green,
`Ready`, and completely unisolated. Anything that checks pod status sees a
healthy deployment; the security story is fake and indistinguishable from a real
one.

**A CNI that ignores NetworkPolicy looks identical to one that enforces it.**
kind's default CNI, `kindnetd`, implements NetworkPolicy not at all. The
policies apply cleanly, `kubectl get networkpolicy` is reassuring, and nothing
is blocked. An early version of the security model claimed NetworkPolicy
enforcement had been verified. It had not been: measured on that cluster, a
`baseline` persona reached Postgres on five consecutive attempts. That took a
CNI swap to fix, and a third gate to prevent recurring.

**Gateway API CRDs can be installed with nothing watching them.** On the first
cluster this ran on, the CRDs were present — installed by Traefik — but there
was no GatewayClass and Traefik had never been started with
`--providers.kubernetesgateway`. The entire Gateway API surface was inert and
looked installed.

**A router can be written, reviewed and never mounted.** The sandbox-facing
internal API — retrieval, artifacts, action items — was complete and correct,
and `include_router` was never called for it. Every handler passed its unit
tests. Every persona tool call would have returned 404. It was caught by the
first integration test that asserted against the assembled application rather
than against the handlers, and the test that catches it now asserts a `401`,
because a `401` proves the route exists while a `404` proves nothing.

The habit that comes out of all four: **provoke the behaviour, observe what
happens**. Not "is the object present", not "is the pod ready" — run the code
and look. Every gate in this repository is written that way, and the
`/proc/version` assertion in particular must never be weakened to a readiness
check.

---

## Legibility beats marginal strength

Two decisions went the same way, and the second only because the first had
already taught the lesson.

**Blocking apiserver egress made the system weaker.** An early version denied
apiserver network access to profiles without the code-execution grant, on the
theory that it was defence in depth. It was marginally stronger and clearly
worse: the refusal became a 60-second connection timeout instead of a fast 403.
A policy decision and an outage became indistinguishable from the outside, and
the turn stalled rather than reporting anything.

Every profile now keeps apiserver reachability. RBAC is the authoritative
control, the refusal comes back as an unambiguous 403, and the agent reports it
as a normal tool result that the audit matrix displays. **A denial nobody can
see is a control nobody can trust.**

**A denial is reported, not raised.** `run_python_analysis` catches the 403 and
returns `DENIED_BY_CLUSTER` as a tool result; the agent carries on contributing.
Raising would have crashed the turn and shown an outage where a policy decision
belongs — misrepresenting the security story in exactly the direction that
flatters it.

The generalisation: once execution moves off-process, observability into *why* a
turn failed matters more, not less. Isolation makes failures quieter.

---

## Knowing what not to distribute

The LangGraph graph never leaves the backend. Sandboxes are turn executors, not
graph participants: the entire ReAct loop for one turn runs remotely and returns
one result, rather than RPC-ing each tool call back individually.

Distributed checkpointing across sandboxes was named early and explicitly scoped
*out* as a research project rather than a demo requirement. That decision is as
load-bearing as the isolation itself, and it paid twice — a four-step turn costs
one round trip instead of four, and model-chosen tool arguments never reach the
backend process at all.

---

## What a real model does to a design that is sound on paper

The stage most tutorials skip. None of these are exotic bugs; all of them are
what "the demo works against a stub" hides.

- An async `httpx` client with a **synchronous** event hook silently swallowed
  every tool call — `await`-ing `None`. It presented as random turn failures and
  stayed mysterious until tracebacks were rendered instead of logged as the
  literal string `true`.
- A reasoning-capable model spent its entire 300-token supervisor budget on
  internal thought and returned truncated JSON every time, so meetings ended
  before they began. Reasoning effort is now configured separately for the chair
  and the agents, because a bounded structured-output call and a full turn want
  opposite things from it.
- An unclosed `<thought>` tag leaked a General Counsel's private reasoning
  straight into the public transcript.
- Small models routinely answer with a name or a title instead of the id they
  were asked for. A single hallucinated name — "Ben", nobody in the room — used
  to end the meeting at turn 0 with an empty transcript, because an unresolvable
  name and a decision to stop both collapsed to `FINISH`. They are now different
  outcomes: unresolvable retries, and then falls back to whoever has not spoken.
- Persona `system_prompt` sat where the prompt *template* belonged, so a persona
  with any notes at all replaced the entire structured template — every
  placeholder, all the tool guidance — with about 200 characters of flavour
  text. That is why agents never called a tool.

---

## Deploys that report success and change nothing

A mutable `:latest` tag leaves the Deployment spec unchanged when the image is
rebuilt. `helm upgrade` finds nothing to roll, the pod keeps serving old code,
and the deploy reports success. This cost real debugging time twice before
images were tagged by content digest.

The same class of silent success showed up in the settings API. Only `get()`
unwrapped the response envelope; `post`/`put`/`patch`/`delete` returned
`response.data.data` unconditionally. Several routes answer HTTP 200 with
`status: "error"`, so a failed mutation resolved as a successful one carrying
`undefined` — and the UI showed a success toast for a write that never happened.

And in durability: the meeting executor used to swallow any Postgres
checkpointer failure and fall through to an in-memory saver, logging a warning.
The meeting then ran with no durability at all, a backend restart lost it, and
nothing in the API or the UI indicated that had happened. Silent downgrades of a
durability guarantee are worse than hard failures. It now refuses to run unless
`ALLOW_VOLATILE_CHECKPOINTS` is explicitly set.

Three instances of one pattern, which is why the same rule now governs operator
authentication: with `AUTH_ENABLED` true and no token configured, the process
does not start.

---

## Tests that pass for the wrong reason

The unit suite silently depended on **no database being reachable**. `turn_cache`
fails soft by design — a cache miss is always safe, so a failed lookup must not
fail a turn — and with no Postgres listening, every lookup missed and every test
passed. Run the same suite on a machine with a database up and turns began being
served from a real table, leaking results between tests as failures in code
nobody had touched.

The suite now stubs the cache per test. The general form: a test that passes
because a dependency is *absent* is not a passing test, it is an untested path
with a green tick next to it.

A related one, from wiring the integration suite: the SQLAlchemy engine is
created at import and its asyncpg pool binds to whichever event loop first uses
it. pytest gives each test its own loop, so a pooled connection carried over
fails with "attached to a different loop" — a message that points at asyncio and
not at the pool actually holding the stale handle.

---

## Sizing a warm pool for the wrong unit

Sandboxes were claimed once per *turn* rather than once per persona per meeting.
It is invisible in a two-attendee test and wrong at any real size: a warm pool is
sized for concurrent speakers, so a four-person meeting running twelve turns
drains a pool of two by the third turn. Every attendee after that pays a cold
gVisor start, or fails outright.

The leaked claims were invisible too. Only the most recent sandbox per agent
survived into the graph state that the end-of-meeting release walked, so
everything earlier sat holding a warm-pool slot until a startup sweep noticed.
The manager now holds the leases and is the record of truth for release, rather
than the event log — which by construction names sandboxes that may already be
gone and may not name their replacements at all.

---

## Configuration hygiene precedes features

The project's original form kept credentials in a plaintext `config.json` on a
PVC — the Postgres password and both model API keys — and re-parsed it from disk
on *every attribute access*, including the status poll each browser tab fires
every few seconds.

Replacing it with environment-only configuration and a `system_settings` table
happened before a single persona feature shipped in the rebuild. Retrofitting
that under a working feature set is a much larger diff, and "production-grade"
claimed after the fact is a claim nobody can check.

---

## Corrections on record

Two conclusions in earlier versions of this documentation were wrong, and are
recorded rather than quietly edited away.

**NetworkPolicy enforcement was claimed before it was measured.** Covered above;
it is the reason the third gate exists.

**Direct sandbox addressing was verified against the wrong object.** An early
Phase 0 note concluded that a pod in `meetings` could reach a sandbox in
`meetings-sandboxes` at its `serviceFQDN`. That was drawn from a Sandbox created
*directly*, with no SandboxTemplate. Sandboxes created from a template behave
differently: `networkPolicyManagement` defaults to `Managed`, and where a
template declares no `networkPolicy` the controller synthesises one allowing
ingress only from the Sandbox Router. Direct calls are denied under that
default. The project now declares its policy explicitly and opens exactly the
two routes it needs.

---

## Where the pattern generalises

Reading the above as a sequence, the argument for Kubernetes-native agent
isolation rebuilds itself in order: establish the kernel boundary and *prove*
it; move execution across it without turning the system into a distributed
computing project; let the cluster rather than the prompt decide privilege;
survive a real model; make the boundary legible from outside; and only then ask
whether you would operate it.

Each stage answers a question that has to be answered before the next one is
worth asking. Skipping any of them produces a demo that works and a claim that
does not hold.
