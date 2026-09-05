# Sandbox security model

What contains untrusted agent execution here, what each control actually stops,
and how to confirm it is doing so on your cluster rather than taking this
document's word for it.

The commands that produce every table below are in
[verify-enforcement.md](verify-enforcement.md). Re-run them after changing a
capability profile.

## Isolation

Agent code runs under gVisor (`runsc`), on the `systrap` platform. The sentry
implements the syscall surface in userspace, so a container escape has to get
through a second kernel implementation before it reaches the host's.

`sandbox.runtimeClassName` is a Helm value and `gvisor` appears in no template.
A cluster that cannot run `runsc` drops to a weaker tier by changing one value —
see [requirements.md](requirements.md#isolation-tiers) for what each tier gives
up. They are not equivalent, and presenting on tier 3 while describing tier 1 is
the one thing this document exists to prevent.

### Isolation is asserted, never assumed

A misconfigured RuntimeClass handler does not fail loudly. On several container
runtimes it silently falls back to `runc`, producing a green, `Ready` pod with
no isolation whatsoever — a fake security story that looks identical to a real
one from the outside.

The gate greps `/proc/version` inside a pod that actually ran, for gVisor's
distinctive sentry string:

```bash
make smoke-gvisor
```

```
kernel: Linux version 4.19.0-gvisor #1 SMP ...
GVISOR-OK
```

**Never weaken this assertion to a readiness check.** Readiness is exactly the
signal that cannot tell the two cases apart.

## Two tiers of sandbox

| | Tier A — persona | Tier B — exec |
|---|---|---|
| Runs | one attendee's ReAct loop | model-authored Python |
| Namespace | `meetings-sandboxes` | `meetings-exec` |
| Lifetime | one meeting | one call, 60s deadline |
| Egress | backend internal API, DNS, apiserver (pinned when `APISERVER_CIDRS` is set) | **none** |
| Claimed by | the backend, from a warm pool | the Tier A pod itself, if RBAC allows |

Tier A no longer reaches the model directly either: it calls the backend's
`/internal/v1/llm` proxy, so a persona sandbox carries no provider credential.
That replaced an ipBlock list of the endpoint's addresses, which a CDN-fronted
provider cannot supply -- the only rule that worked was `0.0.0.0/0`, granting
the whole internet to the least-trusted component in order to reach one host.

Set `APISERVER_CIDRS` in `cluster.env` and the apiserver route is pinned to the
control plane, at which point a persona sandbox genuinely has no route off the
cluster. Leave it empty and the rule falls back to any address on TCP 443 and
6443 -- which is a *port* restriction, not a destination one, and 443 is the
whole HTTPS internet. The sandbox still holds no credential worth taking, but do
not describe that fallback as an egress boundary. This was exactly the shape of
error the rest of this document warns about: a control that reads as closed and
is not.

Tier B is where the strongest statement holds: code the model wrote runs with no
network route to anything. Not to the database, not to the backend, not to the
model.

The Tier B claim is made **by the persona pod**, using the ServiceAccount token
mounted into it. The backend is not in that path and cannot broker around the
apiserver's decision on a persona's behalf.

## The five layers

| Layer | Control | Stops | Observable as |
|---|---|---|---|
| Prompt | Only granted tools are registered | Honest mistakes | the tool list in a bind |
| Runtime | Grant intersected with a capability file mounted from a ConfigMap | A compromised backend over-granting | `/etc/sandbox/capabilities` in the pod |
| Secret | Credentials mounted only into templates that need them | Nothing to steal | file present or absent |
| NetworkPolicy | Default-deny egress per profile | A stolen credential being *used* | a socket that connects or does not |
| RBAC | Only some ServiceAccounts may claim an exec sandbox | The agent itself | a 403 from the apiserver |

Only the last one is decided by a system the application cannot influence at
all. That is why it carries the demo.

### Measured

**Layer 5 — RBAC.** May the profile claim a code-execution sandbox?

| baseline | counsel | strategist | analyst | **quant** | chief |
|---|---|---|---|---|---|
| no | no | no | no | **yes** | no |

`chief` has the broadest tool set of any profile and is still refused. Capability
follows the job, not the rank — and the contrast between `chief` and `quant` is
what makes the boundary legible rather than a rule that happens to bind the CEO.

**Layer 4 — NetworkPolicy.** May the profile reach Postgres on 5432?

| baseline | counsel | analyst | quant | chief |
|---|---|---|---|---|
| blocked | blocked | open | open | open |

**Layer 3 — Secret.** Is a metrics DSN present in the sandbox's filesystem?

| baseline | counsel | analyst | quant | chief |
|---|---|---|---|---|
| absent | absent | present | present | present |

Layers 3 and 4 compose: a persona with no credential also has no route to use
one, so a DSN leaked into a prompt is useless to the wrong profile.

**The same tool, from two profiles, through the real code path:**

```
profile=quant    -> hello from the exec sandbox
profile=counsel  -> DENIED_BY_CLUSTER: this persona is not permitted to
                    execute code. The Kubernetes API server refused the
                    sandbox claim.
```

**What the exec tier can reach, from inside it:**

| database | backend | the model |
|---|---|---|
| blocked | blocked | blocked |

End to end: model-authored matplotlib runs in that network-isolated tier and
returns a 36KB PNG, which becomes a meeting artifact.

## Design decisions worth stating

### Every profile may reach the apiserver

Blocking apiserver egress for profiles without the code-execution grant sounds
like defence in depth. It is marginally stronger and clearly worse: the refusal
becomes a 60-second connection timeout instead of a 403, and a policy decision
becomes indistinguishable from an outage.

RBAC is the authoritative control. Letting the request reach the apiserver means
the refusal comes back fast and unambiguous, the agent can report it, and the
audit matrix can display it. **A denial nobody can see is a control nobody can
trust.**

*Trade-off accepted:* every persona pod can talk to the apiserver, so a
compromised pod can enumerate what its ServiceAccount is permitted to do. It
cannot exceed it, and the alternative traded a visible control for an invisible
one.

### A denial is reported, not raised

`run_python_analysis` catches the 403 and returns `DENIED_BY_CLUSTER` as a
normal tool result. The agent carries on contributing, and the refusal appears
in the transcript and the audit matrix as a policy decision.

*Trade-off accepted:* the agent learns it was refused and could, in principle,
route around the refusal. It has nowhere to route to — every other layer holds —
and the alternative crashes the turn, showing an outage where a policy decision
belongs. That misrepresents the security story in the direction that flatters
it.

### Sandboxes never hold the application database credential

Everything a persona sandbox needs goes through a scoped internal API, and the
caller's identity there comes from a TokenReview plus the pod's own labels — not
from the request body, which the model composes.

The single exception is a read-only DSN for a separate metrics schema, mounted
only into profiles granted it, and reachable only by profiles whose
NetworkPolicy allows it.

*Trade-off accepted:* an extra hop for every retrieval, and a backend that must
be reachable for a sandbox to do useful work. The alternative puts a credential
the model can read inside the boundary the model is being contained by.

### NetworkPolicy is declared explicitly, not managed

Sandboxes created from a SandboxTemplate default to `networkPolicyManagement:
Managed`, where the controller synthesises a policy allowing ingress only from
the Sandbox Router. That default is a good one and assumes traffic goes through
the Router.

This project declares its policy explicitly and opens a short list of routes:
ingress from the backend on 8080, and egress to the backend's internal API on
8000 plus DNS and the apiserver -- and, per profile, Postgres, the corpus, the
exec namespace and the trace collector. Nothing reaches the internet. The
backend runs in-cluster and reaches sandboxes at `status.serviceFQDN` directly,
so the Router is not on the hot path.

*Trade-off accepted:* the policy is ours to maintain, and a new egress
requirement is a chart change rather than something the controller infers. In
exchange the routes are readable in one file, and there is one fewer hop in
every turn.

### One image, many profiles

Every profile runs the same runtime image. Nothing about the image decides what
an agent may do — only the SandboxTemplate, ServiceAccount, NetworkPolicy and
mounted Secrets its sandbox is created from.

*Trade-off accepted:* the image contains code for tools a given profile will
never be allowed to call. That is the point: the runtime intersects the
backend's grant with a capability file mounted from a ConfigMap, so what is
*present* and what is *permitted* are separate questions decided by separate
systems.

## What this model does not claim

- **It is not a defence against a malicious operator.** Anyone holding the
  operator token can grant a persona any profile. The role split
  ([operations.md](operations.md#operator-access)) narrows who that is; it does
  not eliminate the trust.
- **It is not a multi-tenant boundary.** One database, one namespace set, one
  meeting at a time. Personas are isolated from each other and from the host;
  tenants are not a concept here.
- **It does not defend the model itself.** A persona reaches the model through
  the backend's proxy, and the proxy forwards what it is given -- it is a
  network and credential boundary, not a content filter. Prompt injection through retrieved documents is
  in scope for what the *cluster* controls — a compromised agent still cannot
  exceed its profile — and out of scope for what the *model* does within them.
- **Tiers 2 and 3 are weaker, and differently weak.** Without a kernel boundary,
  container escape is a host-kernel bug away. The RBAC and NetworkPolicy layers
  are unaffected; the isolation layer is the one you lose.
