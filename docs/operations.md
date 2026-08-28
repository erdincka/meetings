# Operating this deployment

Configuration, access control, images and observability. For what a cluster must
provide before any of this applies, see [requirements.md](requirements.md).

## Configuration, split by lifetime

Two kinds of setting, deliberately kept apart, because conflating them is how
credentials end up somewhere they can be read.

**Infrastructure** — database URL, model endpoints, API keys, operator tokens,
CORS origins. Environment-only, injected from the `meetings-runtime` and
`meetings-auth` Secrets and the `meetings-config` ConfigMap, read once at
import, never written at runtime. There is no configuration file.

**Operator-tunable** — prompts, turn limits, temperatures, retrieval limits.
Stored in the `system_settings` table and editable through the UI.

The boundary is enforced rather than documented: the settings update schema
forbids extra fields, so an attempt to set a credential through the settings API
is a `422`, not a silently ignored write. A rejected write an operator can see
is the only kind that teaches them anything.

## Operator access

Two roles, split by what a mistake costs.

| Role | May |
|---|---|
| `viewer` | Read meetings, transcripts, artifacts, personas, capability profiles, system status |
| `operator` | Everything a viewer may, plus edit personas and settings, and start or stop meetings |

The split is not the generic read/write one. A persona's tool list resolves to a
capability profile, and the profile decides which ServiceAccount its sandbox
runs under — so **editing a persona is a privilege change**, while reading a
transcript is not. The role boundary is drawn where that difference falls.

Authorisation is derived from the HTTP method rather than declared per route:
safe methods need `viewer`, everything else needs `operator`, applied once to
the router. Enumerating routes invites the opposite failure — a mutating
endpoint added later that nobody remembers to protect. A method-derived rule
fails closed for code that has not been written yet.

### Getting the tokens

Generated on first install and preserved across upgrades:

```bash
make operator-token
```

The chart reads the existing Secret back on upgrade and reuses its values, so a
`helm upgrade` does not rotate every token and lock the room out of the UI
mid-demo. The Secret carries `helm.sh/resource-policy: keep`.

### Rotating a token

```bash
kubectl -n meetings patch secret meetings-auth --type=json \
  -p="[{\"op\":\"replace\",\"path\":\"/data/OPERATOR_TOKEN\",\"value\":\"$(openssl rand -base64 30 | tr -d '\n' | base64)\"}]"
kubectl -n meetings rollout restart deploy/meetings-backend
```

Anyone holding the old token is signed out on their next request: the API
answers `401`, the browser discards the stored token and re-prompts.

### Managing tokens yourself

```yaml
auth:
  existingSecret: my-own-secret
  operatorTokenKey: OPERATOR_TOKEN
  viewerTokenKey: VIEWER_TOKEN
```

The chart then generates nothing.

### Turning authentication off

`auth.enabled=false` makes every API caller an operator. Legitimate for a
single-user development cluster and nowhere else. With `AUTH_ENABLED` true and
no operator token the backend **refuses to start** — the same rule the durable
checkpointer follows, for the same reason: a control that silently degrades to
nothing is worse than one that was never claimed. Switching it off is a single
explicit value, and the backend logs a warning at startup so it cannot be
forgotten quietly.

### What is deliberately unauthenticated

`/health`, `/readyz` and `/metrics` — Kubernetes probes and the Prometheus
scrape hold no credential, and `/metrics` exposes counters, not data.
`/api/v1/auth/config` answers a single boolean, because a client cannot be asked
to authenticate before it can discover that authentication exists.

`/internal/*` is authenticated differently and more strongly: a sandbox presents
its projected ServiceAccount token, validated by the apiserver via TokenReview,
and the caller's identity is read from the pod's labels rather than from
anything in the request body. That distinction is load-bearing — the model
inside the sandbox composes the request body, so an identity taken from there
could be talked into reading another persona's library. The surface is mounted
outside `/api/v1` and is not routed through the Gateway.

## Images and supply chain

A content-addressed tag answers "did this image change?", which is a
cache-correctness question. Before running an image on a cluster you care about,
the question is different: **did this image come from this repository, built
from this commit, by CI, and has anything touched it since?**

Three artefacts answer it, and they are not interchangeable:

| Artefact | Answers |
|---|---|
| cosign signature (keyless) | Signed by *this workflow in this repository*. No private key exists to leak or rotate — the identity is the workflow's OIDC token |
| SLSA provenance attestation | Which commit, which workflow, which builder. A signature says who signed; provenance says what was built and from where |
| SBOM | The package inventory, so a future CVE is answered by query rather than by rebuilding to find out |

```bash
make verify-images
```

Verification is part of an install, not an optional extra: an unverified
signature is decoration. The same command runs in the release workflow against
what it has just pushed, so a release that cannot be verified fails there rather
than on your cluster.

Signatures are made over the **digest**, never the tag. A tag can be moved to
point at something else, and a signature over a movable name proves nothing
about the bytes anyone actually pulls.

Images are built for `linux/amd64` and `linux/arm64` — amd64 because that is
what most clusters run, arm64 because Apple Silicon is the common development
machine and a demo that cannot be run locally is a demo nobody checks.

### Building your own images

For a fork, or an air-gapped cluster. Set `IMAGE_REGISTRY` in `cluster.env` to a
registry your nodes can reach, then:

```bash
make images
make deploy
```

Images are tagged by content digest rather than `:latest`. A mutable tag leaves
the Deployment spec unchanged when the image is rebuilt, so `helm upgrade` finds
nothing to roll and the pod keeps serving stale code — a deploy that reports
success having changed nothing, which is the worst shape of failure because it
is invisible. Tagging by digest makes the tag change exactly when the content
changes, so identical content also does not churn pods.

Setting `BUILDER` to an SSH target builds there instead of locally. That exists
for the case where your workstation and your nodes differ in architecture, where
cross-building Python and Node images under emulation is slow enough to hurt an
inner loop. It is an optimisation; the local path produces identical images.

Self-built images are not signed by this repository's CI, and `make
verify-images` will correctly refuse them.

## Database migrations

Alembic, run as a Helm pre-upgrade hook. Schema changes are explicit and
reviewable rather than a startup side effect, and a failed migration fails the
upgrade instead of leaving a running pod against a schema it does not
understand.

```bash
make migrate-check    # fails if the ORM has drifted from the migrations
```

The drift check runs in CI. It catches the case where a model changes and no
migration is written — which does not fail locally, because the developer's
database was created from the models.

## Observability

Optional and off by default; the app serves metrics and runs normally without
any of it, because telemetry must never be the reason a system fails to start.

```bash
make observability      # Prometheus, Grafana, Tempo -- roughly 1.5GB
make deploy-observed    # redeploy with tracing and scraping enabled
```

One `traceparent` is propagated over the sandbox RPC and again when a persona
pod claims an exec sandbox, so a single meeting turn renders as one trace
spanning all three trust boundaries. That is what makes "slow", "denied" and
"broken" three distinguishable outcomes rather than one indistinguishable pause
— and a system where those three look identical from outside is not one to grant
more autonomy to, however good its isolation is.

Metric labels are low-cardinality by construction: a persona's *profile*, never
its agent id. Denials are counted separately from errors, because collapsing
them would hide the one signal this project exists to surface.

The Grafana dashboard is checked in at
[`dashboards/agentic-meetings.json`](dashboards/agentic-meetings.json) and
provisioned from the repository rather than clicked together.

### Reaching Grafana without a port-forward

Set `OBSERVABILITY_HOST` in `deploy/cluster/cluster.env` to publish Grafana on
the Gateway the app already uses, rather than forwarding a port each time:

```bash
OBSERVABILITY_HOST=grafana.example.com
```

Empty — the default — publishes nothing, and `make observability` prints the
port-forward command instead. The stack is optional, so a listener pointing at a
backend that may not exist is not something to create by default.

It reuses the app's Gateway deliberately. A second Gateway is the tidier
boundary, but it takes a second LoadBalancer address and therefore a second DNS
record; sharing this one means an existing wildcard record already covers it.
The listener admits routes only from the `observability` namespace, by selector,
so the app's own listener stays `from: Same` and nothing else in the cluster can
attach to the hostname.

Both halves read the same variable, and they are applied by different commands:

```bash
make deploy           # creates the listener
make observability    # creates the route, and reports if the listener is absent
```

Order matters only in that the route cannot attach before the listener exists.
`make observability` checks the route's `Accepted` condition rather than
trusting the apply, because an unattached HTTPRoute still exists and still looks
healthy to `kubectl get` — it simply never receives traffic.

## When agents fail but the app is healthy

Every symptom below presented as "the meeting produced nothing" with no
component in an error state and every HTTP request a clean 200. They are worth
recognising by name.

| Symptom in the log | Cause | Fix |
|---|---|---|
| `finish_reason=length`, `output_tokens` equal to the cap | the model reasons before answering and is truncated mid-tool-call | raise `SUPERVISOR_MAX_TOKENS` (default 1500) |
| `tokens=1`, empty content, `finish_reason=stop` | the provider returned a degenerate completion — typically free-tier throttling | nothing client-side; the chair degrades to an unheard attendee |
| `400 ... structured outputs not support` | the provider implements tool calling but not `json_schema` | already handled: the ladder falls to tool calling, then to plain text |
| `<tool_call>` / `<arg_key>` markup in `content` | the model wrote its tool call as text and the serving stack did not parse it | already handled by `salvage_decision` |
| `'utf-8' codec can't decode byte 0x8b` | a gzipped body forwarded with `content-encoding` stripped | fixed in the proxy; `0x1f 0x8b` is the gzip magic number |

The one setting that is a trap: **reasoning effort is provider-specific and
there is no portable value.** `INFERENCE_REASONING_EFFORT` and
`SUPERVISOR_REASONING_EFFORT` default to empty, meaning "send nothing", and that
is almost always right. Ollama accepts `"none"`; OpenAI-style providers take
`low`/`medium`/`high` and reject `"none"`. A value a provider merely *tolerates*
can still break structured output silently — `"none"` on OpenRouter returned a
200 whose tool call never parsed, so the chair picked nobody and the meeting
ended at turn 0.

Start here when a meeting underperforms:

```bash
kubectl -n meetings logs deploy/meetings-backend | grep supervisor_output_unparsed
```

That line carries `finish_reason`, the token count against the cap, and every
rung the call ladder tried with its own reason.


## Sandbox lifecycle

One sandbox per persona per meeting, claimed lazily on the supervisor's first
selection of that attendee — a five-person meeting where two people never speak
should not hold five pods. The sandbox is reused for every turn that persona
takes and released when the meeting ends.

Claiming per turn instead would drain a warm pool sized for concurrent speakers:
a four-person meeting running twelve turns exhausts a pool of two by the third
turn, and every attendee after that pays a cold gVisor start or fails outright.

Nothing durable lives in a sandbox. If a pod is lost mid-meeting, the backend
forgets the lease, claims a replacement, replays the persona bind and re-issues
the turn — once, and only once. The `turn_results` table makes re-issuing safe;
retrying more than once would hammer a genuinely empty pool with every attendee
in sequence and stall the meeting instead of recording a failure the transcript
can show.

Cleanup has three mechanisms, because any one alone leaks pods:

1. Explicit release when a meeting ends, from the lease table.
2. A startup sweep for sandboxes labelled with a meeting that is no longer
   running — a backend killed mid-meeting never reaches step 1.
3. A startup sweep for claims this backend never labelled, which step 2 cannot
   see because it matches on a label only this process applies.

The SandboxTemplate also carries a shutdown policy, so an orphan eventually
reaps itself even if the backend never comes back at all.

### Idle `persona-*` pods are the warm pool, not a leak

With no meeting running, `meetings-sandboxes` still holds one Running pod per
warm replica, and they stay Running for as long as the deployment is up. That is
the warm pool doing its job: it exists so no turn pays a cold gVisor start, and a
pool with nothing in it is a pool that is not working. Their age tracks the last
`helm upgrade`, not the last meeting, which is what makes them look stuck.

The count to expect is the sum of `warmReplicas` across profiles in
`values.yaml` — six by default, since `baseline` keeps two:

```bash
kubectl -n meetings-sandboxes get sandboxes -L sandbox.users.io/meeting-id
```

The label is what separates the two cases, and it is worth checking before
deleting anything:

- **Empty `MEETING-ID`** — a warm-pool sandbox, waiting to be claimed. Leave it.
  Deleting one only makes the controller build a replacement.
- **A meeting id that is not running** — a genuine orphan, from a backend killed
  mid-meeting. The startup sweep takes these on the next restart; nothing else
  will, because the sweep deliberately matches only on this label.

`warmReplicas` can be tuned per profile in
`backend/app/orchestration/profiles.py` (regenerate with
`scripts/generate-profile-values.sh`), but it cannot go below `1`, and the
dataclass refuses to construct a profile that tries.

There is no cold-start path to fall back on. A sandbox is only ever obtained
through a SandboxClaim, and `SandboxClaim.spec` carries `warmPoolRef` and no
template reference — so a profile with no warm pool has no route to a sandbox
at all. It stays selectable, which is what makes the failure expensive: the
supervisor picks it, the claim fails with `SandboxWarmPool "persona-<name>" not
found`, the turn is consumed, and the next turn picks it again. A meeting can
burn its whole budget this way without producing a single utterance.

## Scaling limits

The backend is pinned to one replica. The active-meeting registry is
process-local, so a second replica would not see meetings owned by the first.
Lifting this needs lease-based ownership; it is a known limit rather than an
oversight, and only one meeting runs at a time by design.

Warm pool sizes are per profile in `sandbox.profiles.*.warmReplicas`. They are
the main lever on both idle footprint and first-turn latency.
