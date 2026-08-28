#!/usr/bin/env bash
# Verify that published images came from this repository's CI, unaltered.
#
# Run it before trusting an image, not after: the point of a signature is to be
# checked by the party taking the risk, and an unverified signature is
# decoration. This is the same command CI runs against what it has just pushed,
# so a release that cannot be verified fails there rather than on your cluster.
#
#   scripts/verify-images.sh                       # every published image, :latest
#   scripts/verify-images.sh <ref>[ <ref>...]      # specific refs or digests
#
# What is checked, and why each one is not enough on its own:
#
#   cosign verify              the image was signed by a workflow in this
#                              repository, keyless, via its OIDC identity
#   cosign verify-attestation  a SLSA provenance statement exists and is signed
#                              by the same identity -- a signature says who
#                              signed, provenance says what was built and from
#                              where
#
# Keyless verification needs both the identity *and* the issuer. Pinning only
# the identity regex would accept a token from any issuer that happens to mint a
# matching subject.
set -euo pipefail
export PATH="$HOME/.rd/bin:/opt/homebrew/bin:$PATH"

REGISTRY=${REGISTRY:-ghcr.io}
OWNER=${OWNER:-erdincka}
EXPECTED_REPO=${EXPECTED_REPO:-erdincka/meetings}
OIDC_ISSUER=${OIDC_ISSUER:-https://token.actions.githubusercontent.com}
TAG=${TAG:-latest}

IMAGES="meetings-backend
meetings-frontend
meetings-persona-runtime
meetings-exec-python
meetings-corpus"

command -v cosign >/dev/null || {
  echo "cosign not found. Install it: brew install cosign" >&2
  exit 1
}

# Any workflow in the repository, on any ref. Tightening this to a single
# workflow file is a reasonable next step for a production deployment; it is
# left broad here so a fork's CI can verify its own builds without editing the
# script.
IDENTITY_REGEX="^https://github.com/${EXPECTED_REPO}/"

refs=("$@")
if [ ${#refs[@]} -eq 0 ]; then
  for image in $IMAGES; do
    refs+=("${REGISTRY}/${OWNER}/${image}:${TAG}")
  done
fi

failed=0
for ref in "${refs[@]}"; do
  printf '\n\033[1m>> %s\033[0m\n' "$ref"

  if cosign verify \
      --certificate-identity-regexp "$IDENTITY_REGEX" \
      --certificate-oidc-issuer "$OIDC_ISSUER" \
      "$ref" >/dev/null 2>&1; then
    echo "   signature   ok"
  else
    echo "   signature   FAILED"
    failed=1
    continue
  fi

  if cosign verify-attestation \
      --type slsaprovenance \
      --certificate-identity-regexp "$IDENTITY_REGEX" \
      --certificate-oidc-issuer "$OIDC_ISSUER" \
      "$ref" >/dev/null 2>&1; then
    echo "   provenance  ok"
  else
    echo "   provenance  FAILED"
    failed=1
  fi
done

echo
if [ "$failed" -ne 0 ]; then
  echo "verification FAILED -- do not deploy these images" >&2
  exit 1
fi
echo "all images verified: signed by ${EXPECTED_REPO} CI, with build provenance"
