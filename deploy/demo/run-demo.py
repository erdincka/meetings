"""Drive the least-privilege demo meeting to completion.

Runs as a Job inside the cluster rather than over `kubectl exec`. A meeting takes
minutes, the WebSocket disconnect cancels it, and an exec stream that drops --
which it does -- takes the meeting with it. In-cluster, nothing outside the
cluster has to stay connected.

The scenario is chosen to make the enforcement boundary visible: the Finance
Director resolves to the `quant` profile and may execute code, while the General
Counsel resolves to `counsel` and may not. Both are asked to analyse the defect
data.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request

BASE = os.getenv("BACKEND_URL", "http://meetings-backend.meetings.svc:8000/api/v1")
TURN_LIMIT = int(os.getenv("TURN_LIMIT", "4"))

# The operator token, mounted from the meetings-auth Secret. Creating a meeting
# and driving it are operator actions, not viewer ones: they spend cluster
# resources and drive real model calls.
TOKEN = os.getenv("OPERATOR_TOKEN", "")
AUTH = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


def get(path: str) -> object:
    request = urllib.request.Request(BASE + path, headers=AUTH)
    return json.loads(urllib.request.urlopen(request, timeout=60).read())["data"]


def post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **AUTH},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(request, timeout=120).read())["data"]


def create_meeting() -> str:
    roles = {r["title"]: r for r in get("/roles")}
    for required in ("FD", "GC"):
        if required not in roles:
            print(f"missing persona {required}; run the seeder first", file=sys.stderr)
            raise SystemExit(1)

    meeting = post(
        "/meetings",
        {
            "brief": (
                "Product P-200 (Aurora Regulator Pro) shows a rising defect rate across "
                "FY25. Batch 842 is the current shipment."
            ),
            "objective": "Decide whether to recall batch 842.",
            "agenda": (
                "1. Quantify the defect trend from the metrics warehouse\n"
                "2. Legal exposure and disclosure duties\n"
                "3. Decision and owner"
            ),
            "expectations": "A recall decision, supported by the actual numbers.",
            "selected_attendee_ids": [roles["FD"]["id"], roles["GC"]["id"]],
            "turn_limit": TURN_LIMIT,
        },
    )
    return str(meeting["id"])


async def run(meeting_id: str) -> int:
    import websockets

    url = BASE.replace("http://", "ws://").replace("https://", "wss://")
    counts: dict[str, int] = {}
    denials: list[str] = []

    # The token travels as a subprotocol, the same way the browser sends it: a
    # WebSocket handshake carries no Authorization header, and a query string
    # would put the token in every proxy access log between here and the pod.
    subprotocols = ["bearer", TOKEN] if TOKEN else None

    async with websockets.connect(
        f"{url}/meetings/{meeting_id}/ws",
        ping_interval=20,
        max_size=None,
        subprotocols=subprotocols,
    ) as ws:
        await ws.send(json.dumps({"command": "start_meeting"}))
        while True:
            try:
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=900))
            except TimeoutError:
                print("timed out waiting for the next event", file=sys.stderr)
                return 1

            kind = str(event.get("type"))
            counts[kind] = counts.get(kind, 0) + 1

            if kind == "agent_spoke":
                print(f"\n[{event.get('sandbox')}]")
                print(f"  {str(event.get('content'))[:400]}")
                for entry in event.get("tool_audit") or []:
                    if entry.get("denied_reason"):
                        denials.append(f"{entry['agent_id']}: {entry['tool']}")
                        print(f"  >> DENIED {entry['tool']}: {entry['denied_reason'][:120]}")
                    elif entry.get("tool"):
                        print(f"  >> used {entry['tool']}")
            elif kind in ("agent_failed", "error"):
                print(f"[{kind}] {str(event.get('content'))[:250]}")
            elif kind == "meeting_completed":
                break

    print(f"\nevents: {counts}")
    print(f"denials: {denials or 'none'}")
    return 0


if __name__ == "__main__":
    mid = sys.argv[1] if len(sys.argv) > 1 else create_meeting()
    print(f"meeting: {mid}")
    raise SystemExit(asyncio.run(run(mid)))
