"""Minting scoped tokens for the Sandbox Router.

The router verifies and never mints: it holds public keys only. Whoever claims a
sandbox is expected to issue the credential that reaches it, and upstream ships
the verifier and a Go helper but no issuer. This is that issuer.

A token authorises exactly one target -- namespace, sandbox name, Sandbox UID,
port, method and path -- until it expires. The UID is what makes it worth
having: a name can be reused by a later sandbox, a UID cannot, so a token minted
for one persona's sandbox does not open whichever pod inherits the name.

The wire format is upstream's scoped-token v2 and must match it byte for byte
where the signature is concerned::

    v2.<kid>.<base64url(claims)>.<base64url(signature)>

with the signature over ``agent-sandbox/scoped-token/v2.<kid>.<payload>``. See
sandbox-router/authz/scopedtoken_v2.go in kubernetes-sigs/agent-sandbox.

Query strings and bodies are not signed. A token for ``POST /v1/turn`` says
nothing about what the turn contains, which is fine here because the holder is
the component that composes the request anyway.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

SIGNING_CONTEXT = "agent-sandbox/scoped-token/v2."
_KEY_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ScopedTokenError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    """What one token authorises. Mirrors upstream's AuthorizationTarget."""

    namespace: str
    sandbox_name: str
    sandbox_uid: str
    port: int
    method: str
    path: str


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _normalise_path(path: str) -> str:
    """The path as the router will compare it.

    The router unescapes and re-escapes the path it received before comparing,
    so a claim that is not already in that form can never match. Go's
    EscapedPath and ``quote`` agree on everything this project sends.
    """
    if not path:
        return "/"
    if not path.startswith("/"):
        raise ScopedTokenError("path must be absolute")
    return quote(unquote(path), safe="/-_.~!$&'()*+,;=:@")


class ScopedTokenMinter:
    def __init__(self, private_key: Ed25519PrivateKey, key_id: str) -> None:
        if not _KEY_ID.match(key_id):
            raise ScopedTokenError(f"invalid key ID {key_id!r}")
        self._key = private_key
        self.key_id = key_id

    @classmethod
    def from_pem_file(cls, path: str | Path, key_id: str) -> ScopedTokenMinter:
        key = load_pem_private_key(Path(path).read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ScopedTokenError("router signing key must be Ed25519")
        return cls(key, key_id)

    def mint(self, target: Target, ttl_seconds: int) -> str:
        if not target.sandbox_uid:
            # The router rejects a v2 token with no UID. Refusing here keeps the
            # failure next to its cause instead of surfacing as a 401 later.
            raise ScopedTokenError("Sandbox UID is required")
        if ttl_seconds < 1:
            raise ScopedTokenError("ttl must be at least one second")
        claims = {
            "ns": target.namespace,
            "name": target.sandbox_name,
            "uid": target.sandbox_uid,
            "port": target.port,
            "method": target.method.strip().upper(),
            "path": _normalise_path(target.path),
            "exp": int(time.time()) + ttl_seconds,
        }
        payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
        signature = self._key.sign(f"{SIGNING_CONTEXT}{self.key_id}.{payload}".encode())
        return f"v2.{self.key_id}.{payload}.{_b64(signature)}"
