#!/usr/bin/env bash
# Write the inference credentials from cluster.env into the `meetings-runtime`
# Secret, which is what the chart's inference.existingSecret points at.
#
# Applied here rather than templated by Helm, deliberately. A key passed through
# `--set` lands in the release Secret and comes back out of `helm get values` in
# plaintext for anyone with read access to the namespace -- so the chart never
# renders a credential, and this closes the gap that left by giving the keys a
# documented home in cluster.env instead of a manual kubectl command nobody
# could find.
#
# Idempotent, and safe with empty values: a local provider that needs no
# authentication gets an empty key rather than a missing Secret, because the
# code sends a placeholder and a missing Secret would fail the mount.
set -euo pipefail
export PATH="$HOME/.rd/bin:/opt/homebrew/bin:$PATH"

REPO=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE="$REPO/deploy/cluster/cluster.env"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

NS=${NAMESPACE:-meetings}
NAME=${RUNTIME_SECRET:-meetings-runtime}
KUBECTL=${KUBECTL:-kubectl}
[ -n "${KCTX:-}" ] && KUBECTL="$KUBECTL --context $KCTX"

$KUBECTL create namespace "$NS" --dry-run=client -o yaml | $KUBECTL apply -f - >/dev/null

$KUBECTL create secret generic "$NAME" -n "$NS" \
  --from-literal=INFERENCE_API_KEY="${INFERENCE_API_KEY:-}" \
  --from-literal=EMBEDDING_API_KEY="${EMBEDDING_API_KEY:-}" \
  --dry-run=client -o yaml | $KUBECTL apply -f - >/dev/null

if [ -n "${INFERENCE_API_KEY:-}" ]; then
  echo "  inference credentials applied to $NS/$NAME"
else
  echo "  $NS/$NAME applied with no inference key (fine for a local provider)"
fi
