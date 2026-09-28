"""The router token must verify the way upstream's Go verifier does.

These re-implement the verification steps from
sandbox-router/authz/scopedtoken.go rather than trusting the minter to check
itself: a minter and a test that share a mistake agree with each other and with
nothing else.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.sandbox.scoped_token import ScopedTokenError, ScopedTokenMinter, Target

TARGET = Target(
    namespace="meetings-sandboxes",
    sandbox_name="persona-quant-abc12",
    sandbox_uid="0f7c1c1e-3a55-4c0e-9a57-2f1d6b0f4e11",
    port=8080,
    method="post",
    path="/v1/turn",
)


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_token_verifies_with_the_public_key_and_upstream_signing_input() -> None:
    key = Ed25519PrivateKey.generate()
    token = ScopedTokenMinter(key, "meetings").mint(TARGET, 60)

    version, kid, payload, signature = token.split(".")
    assert (version, kid) == ("v2", "meetings")
    # Raises InvalidSignature if the signing input differs from upstream's.
    key.public_key().verify(
        _unb64(signature), f"agent-sandbox/scoped-token/v2.{kid}.{payload}".encode()
    )


def test_claims_carry_the_whole_target_normalised() -> None:
    token = ScopedTokenMinter(Ed25519PrivateKey.generate(), "k1").mint(TARGET, 60)
    claims = json.loads(_unb64(token.split(".")[2]))

    assert claims["ns"] == TARGET.namespace
    assert claims["name"] == TARGET.sandbox_name
    assert claims["uid"] == TARGET.sandbox_uid
    assert claims["port"] == 8080
    assert claims["method"] == "POST"
    assert claims["path"] == "/v1/turn"
    assert 0 < claims["exp"] - int(time.time()) <= 60


def test_padding_is_stripped() -> None:
    token = ScopedTokenMinter(Ed25519PrivateKey.generate(), "k1").mint(TARGET, 60)
    assert "=" not in token


def test_a_token_without_a_uid_is_refused_at_mint() -> None:
    minter = ScopedTokenMinter(Ed25519PrivateKey.generate(), "k1")
    with pytest.raises(ScopedTokenError):
        minter.mint(Target("ns", "name", "", 8080, "POST", "/run"), 60)


@pytest.mark.parametrize("kid", ["", "has.dot", "has space", "x" * 129])
def test_key_ids_the_router_would_reject_are_refused(kid: str) -> None:
    with pytest.raises(ScopedTokenError):
        ScopedTokenMinter(Ed25519PrivateKey.generate(), kid)
