#!/usr/bin/env bash
# Resolve each image to the tag `make deploy` should use, and optionally push.
#
#   scripts/image-tags.sh          emit tag assignments for `make deploy`
#   scripts/image-tags.sh --push   also tag and push the content-addressed tags
#
# Why not a plain `:latest` for images you build yourself: a mutable tag leaves
# the Deployment spec unchanged when the image is rebuilt, so `helm upgrade`
# finds nothing to roll and the pod keeps serving stale code. The deploy reports
# success having changed nothing, which is the worst shape of failure because it
# is invisible.
#
# Tagging by content digest makes the tag change exactly when the image changes:
# same content, same tag, no pointless restart; new content, new tag, guaranteed
# rollout.
#
# Published releases need none of this. Their tags are already immutable and
# signed, so if no locally built image is found this falls back to
# IMAGE_TAG from cluster.env and says so.
#
# Written for bash 3.2, which is what macOS ships: no mapfile, no associative
# arrays.
set -euo pipefail
export PATH="$HOME/.rd/bin:/opt/homebrew/bin:$PATH"

REPO=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE="$REPO/deploy/cluster/cluster.env"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

REGISTRY=${IMAGE_REGISTRY:?set IMAGE_REGISTRY in deploy/cluster/cluster.env}
TAG=${IMAGE_TAG:-latest}
# shellcheck source=scripts/builder-target.sh
. "$REPO/scripts/builder-target.sh"
BUILDER=$(normalize_builder "${BUILDER:-}")
PUSH=${1:-}

IMAGES="BACKEND_TAG:meetings-backend
FRONTEND_TAG:meetings-frontend
RUNTIME_TAG:meetings-persona-runtime
EXEC_TAG:meetings-exec-python
CORPUS_TAG:meetings-corpus"

# One round trip for every digest rather than one per image: this runs on every
# deploy, and over SSH the latency adds up.
query=""
for entry in $IMAGES; do
  image="${entry#*:}"
  query="${query}docker image inspect $REGISTRY/$image:latest --format '$image {{.Id}}' 2>/dev/null || echo '$image MISSING';"
done

if [ -n "$BUILDER" ]; then
  results=$(ssh -o BatchMode=yes "$BUILDER" "$query" 2>/dev/null || true)
else
  results=$(eval "$query" 2>/dev/null || true)
fi

digest_of() {
  echo "$results" | awk -v want="$1" '$1 == want { print $2; exit }'
}

push_cmd=""
built=0
for entry in $IMAGES; do
  var="${entry%%:*}"; image="${entry#*:}"
  id=$(digest_of "$image")
  if [ -z "$id" ] || [ "$id" = "MISSING" ]; then
    # Nothing built here: this deployment pulls a published image.
    echo "${var}=${REGISTRY}/${image}:${TAG}"
    continue
  fi
  built=$((built + 1))
  short=$(echo "${id#sha256:}" | cut -c1-12)
  echo "${var}=${REGISTRY}/${image}:${short}"
  push_cmd="${push_cmd}docker tag $REGISTRY/$image:latest $REGISTRY/$image:$short;docker push -q $REGISTRY/$image:$short >/dev/null;"
done

if [ "$built" -eq 0 ]; then
  echo "no locally built images found; using published ${REGISTRY}/*:${TAG}" >&2
  echo "verify them before deploying:  make verify-images" >&2
fi

if [ "$PUSH" = "--push" ] && [ -n "$push_cmd" ]; then
  if [ -n "$BUILDER" ]; then
    ssh -o BatchMode=yes "$BUILDER" "$push_cmd"
  else
    eval "$push_cmd"
  fi
  echo "pushed content-tagged images to $REGISTRY" >&2
fi
