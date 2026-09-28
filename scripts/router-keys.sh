#!/usr/bin/env bash
# Create the Sandbox Router's signing keypair, once.
#
# Two Secrets, because the two halves go to different places and the point of
# the split is that they never meet:
#
#   meetings-router-signing       the Ed25519 private key, mounted into the
#                                 backend, which mints tokens
#   meetings-router-verification  the public key, mounted into the routers,
#                                 which verify them and cannot mint
#
# Applied here rather than templated by Helm, for the same reason as the
# inference credentials: a key rendered by the chart lands in the release
# Secret and comes back out of `helm get values`.
#
# Idempotent. An existing signing Secret is left alone -- regenerating on every
# deploy would invalidate tokens in flight and restart nothing, so the routers
# would go on trusting a key nobody holds. To rotate: add a second entry to
# keys.json under a new key ID, roll the routers, switch the backend to the new
# key, then remove the old entry.
set -euo pipefail
export PATH="$HOME/.rd/bin:/opt/homebrew/bin:$PATH"

REPO=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE="$REPO/deploy/cluster/cluster.env"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

NS=${NAMESPACE:-meetings}
SIGNING=${ROUTER_SIGNING_SECRET:-meetings-router-signing}
VERIFICATION=${ROUTER_VERIFICATION_SECRET:-meetings-router-verification}
KEY_ID=${ROUTER_KEY_ID:-meetings}
KUBECTL=${KUBECTL:-kubectl}
[ -n "${KCTX:-}" ] && KUBECTL="$KUBECTL --context $KCTX"

$KUBECTL create namespace "$NS" --dry-run=client -o yaml | $KUBECTL apply -f - >/dev/null

if $KUBECTL -n "$NS" get secret "$SIGNING" >/dev/null 2>&1 &&
   $KUBECTL -n "$NS" get secret "$VERIFICATION" >/dev/null 2>&1; then
  echo "  router keys already present in $NS (key ID: $KEY_ID)"
  exit 0
fi

command -v uv >/dev/null || { echo "uv not found on PATH" >&2; exit 1; }

# Never written to the repository or left behind: the private key exists on
# disk only for as long as it takes kubectl to read it.
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
chmod 700 "$WORK"

# Python rather than openssl: the LibreSSL that ships with macOS cannot
# generate Ed25519 keys, and the backend already depends on `cryptography`.
(cd "$REPO/backend" && uv run --quiet python - "$WORK" "$KEY_ID" <<'PY'
import base64, json, sys
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

work, key_id = Path(sys.argv[1]), sys.argv[2]
key = Ed25519PrivateKey.generate()
(work / "signing-key.pem").write_bytes(
    key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
)
public = key.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw
)
# Unpadded base64url, which is what the router's key file requires.
encoded = base64.urlsafe_b64encode(public).rstrip(b"=").decode()
(work / "keys.json").write_text(json.dumps({"keys": [{"kid": key_id, "publicKey": encoded}]}))
PY
)

$KUBECTL create secret generic "$SIGNING" -n "$NS" \
  --from-file=signing-key.pem="$WORK/signing-key.pem" \
  --dry-run=client -o yaml | $KUBECTL apply -f - >/dev/null
$KUBECTL create secret generic "$VERIFICATION" -n "$NS" \
  --from-file=keys.json="$WORK/keys.json" \
  --dry-run=client -o yaml | $KUBECTL apply -f - >/dev/null

echo "  router keypair created in $NS (key ID: $KEY_ID)"
