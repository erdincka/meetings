"""GATE 4: the Sandbox Router refuses what it should, observed from the backend.

Run inside the backend pod by smoke-router.sh, because the backend is the one
component that both holds the signing key and is allowed to reach the router.

Every case sends a real request and reports what came back. The request is a
POST to /v1/persona with an empty body, so a call that reaches a sandbox is
answered 422 by the sandbox itself -- proof it arrived, without binding a
persona into a warm pod.

Not /healthz. The router answers /healthz on its own proxy port, without
consulting the authorizer, so every case returns 200 whatever the token says.
The first version of this gate probed /healthz and reported a router that
refused nothing as one that admitted everything -- correctly, about a question
nobody was asking.
"""

import asyncio
import json
import sys

import httpx

from app.core.config import settings
from app.sandbox import kube
from app.sandbox.manager import manager

REACHED = 422
PATH = "/v1/persona"

EXPECTED = {
    "direct, bypassing the router": "blocked",
    "router, no token": 401,
    "router, valid token": REACHED,
    "router, token for another sandbox": 403,
    "router, token for another path": 403,
    "router, tampered signature": 401,
    "exec router, from the backend": "blocked",
}


async def main() -> int:
    client = await kube._load()
    namespace = settings.SANDBOX_NAMESPACE
    listing = await client.CustomObjectsApi().list_namespaced_custom_object(
        "agents.x-k8s.io", "v1beta1", namespace, "sandboxes"
    )
    sandboxes = [s for s in listing["items"] if s.get("status", {}).get("podIPs")]
    if len(sandboxes) < 2:
        print("FAIL: need two running sandboxes; are the warm pools ready?")
        return 1
    mine, other = sandboxes[0], sandboxes[1]
    router = settings.SANDBOX_ROUTER_URL

    def headers(target: dict, path: str = PATH) -> dict[str, str]:
        return manager.route_headers(
            namespace=namespace,
            sandbox_name=target["metadata"]["name"],
            sandbox_uid=target["metadata"]["uid"],
            method="POST",
            path=path,
        )

    def readdressed(to: dict) -> dict[str, str]:
        """A token minted for one sandbox, presented for another."""
        h = headers(mine)
        h["X-Sandbox-ID"] = to["metadata"]["name"]
        h["X-Sandbox-UID"] = to["metadata"]["uid"]
        return h

    def tampered() -> dict[str, str]:
        h = headers(mine)
        h["Authorization"] = h["Authorization"][:-4] + "AAAA"
        return h

    unauthenticated = {k: v for k, v in headers(mine).items() if k != "Authorization"}
    cases = {
        "direct, bypassing the router": (f"http://{mine['status']['podIPs'][0]}:8080", {}),
        "router, no token": (router, unauthenticated),
        "router, valid token": (router, headers(mine)),
        "router, token for another sandbox": (router, readdressed(other)),
        "router, token for another path": (router, headers(mine, "/v1/turn")),
        "router, tampered signature": (router, tampered()),
        "exec router, from the backend": (settings.SANDBOX_EXEC_ROUTER_URL, headers(mine)),
    }

    failed = 0
    async with httpx.AsyncClient(timeout=6) as http:
        for name, (base, hdrs) in cases.items():
            try:
                got: object = (await http.post(f"{base}{PATH}", headers=hdrs, json={})).status_code
            except httpx.HTTPError:
                got = "blocked"
            ok = got == EXPECTED[name]
            failed += not ok
            want = "" if ok else f" (want {EXPECTED[name]})"
            print(f"  {'ok  ' if ok else 'FAIL'} {name:<36} {got}{want}")
    print(json.dumps({"failed": failed}))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
