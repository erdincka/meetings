# Demo script

A fifteen-minute walkthrough. The order matters: it builds from "this is a
multi-agent meeting" to "the boundary is real and the API server enforces it",
and every claim is provoked live rather than asserted.

Assumes a deployed, seeded cluster (`make deploy && make seed`) and the
operator token from `make operator-token`. Substitute your own gateway address
for `<gateway>` throughout.

---

## 0. Before the audience arrives

Warm the pools and the model. A cold gVisor pod takes seconds and a cold model
takes longer; neither is interesting to watch.

```bash
make preflight                            # the controls still hold
kubectl -n meetings-sandboxes get pods    # every persona pod 1/1 Running
```

Then open two terminals beside the browser: one for `kubectl`, one for logs, and
sign in to the UI before the audience arrives — a token prompt is not the first
thing you want on screen.

---

## 1. The premise (2 min)

Open the app and create a meeting from **Crisis Incident Response** with four
attendees: **Chief Executive Officer**, **Finance Director**, **General
Counsel**, **Quality Manager**.

Say what the demo is: a meeting of LLM personas, each running its own agent
loop, each in its own sandbox, with different permissions. The interesting part
is not that they talk. It is what happens when one of them tries to act.

Point out the attendee list before starting. Each persona resolves to a
capability profile — visible in the **Capabilities** tab of the persona editor,
and per-meeting at:

```bash
curl -s -H "Authorization: Bearer $(make -s operator-token | sed -n 's/^operator: //p')" \
  "http://<gateway>/api/v1/meetings/<meeting-id>/capabilities" \
  | jq '.data.attendees[] | {display_name, profile, can_execute_code}'
```

The Finance Director resolves to `quant` and may execute code. The General
Counsel resolves to `counsel` and may not. Nobody has told the model this.

---

## 2. One pod per attendee (3 min)

Start the meeting. In the kubectl terminal:

```bash
kubectl -n meetings-sandboxes get pods -w
```

As the chair selects each speaker, a sandbox is claimed from the warm pool and
bound to that persona. The transcript panel shows who is speaking; the pod list
shows where.

Confirm the isolation is real rather than declared:

```bash
kubectl -n meetings-sandboxes exec <a persona pod> -- cat /proc/version
```

Expect `gvisor` in the output. A misconfigured RuntimeClass silently falls back
to `runc` and yields a green pod with no boundary, which is exactly the failure
this command exists to catch.

---

## 3. The agents reach for tools (3 min)

Watch a turn in the runtime log:

```bash
kubectl -n meetings-sandboxes logs -f <the Finance Director's pod>
```

Look for `turn_complete` with a non-zero `tools` count, and the `tool.call`
events in the transcript. The agent was not told to call a tool for this turn;
it was told what its tools are for, and it decided.

This is worth dwelling on, because it is the difference between a demo that
prints a scripted denial and one where the denial is provoked by the agent's
own choice.

> Small models are inconsistent here. If a turn passes with `tools=0`, keep
> going -- later turns and the metrics-owning personas call tools far more
> readily than the baseline ones. Do not restage it as a scripted call; the
> honest version is more convincing than the reliable one.

---

## 4. The denial (4 min) — the centrepiece

The General Counsel has `check_policy_compliance` but not `run_python_analysis`.
Ask the meeting a question that tempts it toward analysis, or force the control
directly:

```bash
kubectl auth can-i create sandboxclaims.extensions.agents.x-k8s.io \
  --as=system:serviceaccount:meetings-sandboxes:persona-counsel \
  -n meetings-exec
```

Expect `no`. Then the same for the Finance Director:

```bash
kubectl auth can-i create sandboxclaims.extensions.agents.x-k8s.io \
  --as=system:serviceaccount:meetings-sandboxes:persona-quant \
  -n meetings-exec
```

Expect `yes`.

Make the point explicitly: **the prompt is not the control.** If the model were
jailbroken into calling the tool, the API server would still refuse. The same
image runs in every persona pod -- what differs is the ServiceAccount, the RBAC
binding, the NetworkPolicy and the secrets mounted into it.

Show the network layer too, since it is the one people assume is decorative:

```bash
# baseline cannot reach the database; analyst can
kubectl -n meetings-sandboxes exec <a baseline pod> -- \
  timeout 5 nc -z meetings-postgres-rw.meetings.svc 5432; echo "exit=$?"
```

The full matrix is in [verify-enforcement.md](verify-enforcement.md).

---

## 5. The audit trail (2 min)

Open the **Capability Matrix** in the meeting view: granted, used, and denied,
per persona. A denial appears as a denial, not as a tool failure -- the runtime
distinguishes a policy refusal from an outage, because showing an outage as a
denial would misrepresent the security story in exactly the direction that
flatters it.

---

## 6. Portability (2 min)

Two claims, both one command each.

**Any inference provider.** Point at a different OpenAI-compatible endpoint and
redeploy; nothing in the application changes:

```bash
helm upgrade meetings deploy/charts/meetings -n meetings \
  -f deploy/charts/meetings/values-cluster.yaml \
  --set inference.endpoint=https://... --set inference.modelName=...
```

**Any conformant cluster.** The same chart, and a check that says whether the
cluster can actually support the controls rather than assuming it:

```bash
make preflight
```

Worth showing if the audience is a platform team — it is the difference between
a demo that runs here and a pattern they can adopt.

---

## 7. The supply chain (1 min)

For an audience that will ask how the images got there:

```bash
make verify-images
```

Signed by this repository's CI, keyless — the identity is the workflow, so there
is no private key to leak — with a SLSA provenance attestation naming the commit
each image was built from. Signatures are over the digest, never the tag: a tag
can be moved, and a signature over a movable name proves nothing about the bytes
anyone pulls.

---

## If something goes wrong

**A turn fails.** The meeting continues and the transcript records the failure
rather than showing silence. Say so and move on; that behaviour is deliberate.

**The first meeting on a fresh cluster is slow.** The checkpointer sets up at
app startup and the model loads on first use. Warm both before demoing.

**The chair ends the meeting early.** Small models sometimes cannot name a
valid speaker. The supervisor retries and then falls back to whoever has not
spoken; if it still finishes, restart the meeting rather than explaining it.

**Nothing schedules.** Check node pressure — sandboxes have real requests and
the warm pools hold several pods idle.

**The UI asks for a token.** `make operator-token`. Sign in before the audience
arrives.

**A control does not behave as described.** Run `make preflight`. A RuntimeClass
that fell back to runc, or a CNI that accepts NetworkPolicy without enforcing
it, both look completely healthy from every other angle — which is exactly why
that command exists.
