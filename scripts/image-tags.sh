#!/usr/bin/env bash
# Resolve each image to a content-addressed tag, and optionally push it.
#
# Why not `:latest`: a mutable tag leaves the Deployment spec unchanged when the
# image is rebuilt, so `helm upgrade` finds nothing to roll and the pod keeps
# serving old code. The deploy reports success having changed nothing, which is
# a bad failure because it is invisible -- it cost real debugging time twice
# before this existed.
#
# Tagging by content digest makes the tag change exactly when the image changes:
# same content, same tag, no pointless restart; new content, new tag, guaranteed
# rollout.
#
#   scripts/image-tags.sh          emit tag assignments for `make deploy`
#   scripts/image-tags.sh --push   also tag and push them from the builder
#
# Written for bash 3.2, which is what macOS ships: no mapfile, no associative
# arrays.
set -euo pipefail
export PATH="$HOME/.rd/bin:/opt/homebrew/bin:$PATH"

BUILDER=${BUILDER:-ubuntu@10.1.1.33}
REGISTRY=${REGISTRY:-10.1.1.240:5000}
PUSH=${1:-}

IMAGES="BACKEND_TAG:meetings-backend
FRONTEND_TAG:meetings-frontend
RUNTIME_TAG:meetings-persona-runtime
EXEC_TAG:meetings-exec-python
CORPUS_TAG:meetings-corpus"

# One SSH round trip for all digests rather than one per image: this runs on
# every deploy and the latency adds up.
query=""
echo "$IMAGES" | while read -r _; do :; done
for entry in $IMAGES; do
  image="${entry#*:}"
  query="${query}docker image inspect $REGISTRY/$image:latest --format '$image {{.Id}}' 2>/dev/null || echo '$image MISSING';"
done
results=$(ssh -o BatchMode=yes "$BUILDER" "$query")

digest_of() {
  echo "$results" | awk -v want="$1" '$1 == want { print $2; exit }'
}

push_cmd=""
for entry in $IMAGES; do
  var="${entry%%:*}"; image="${entry#*:}"
  id=$(digest_of "$image")
  if [ -z "$id" ] || [ "$id" = "MISSING" ]; then
    echo "error: $REGISTRY/$image:latest not built -- run 'make images'" >&2
    exit 1
  fi
  short=$(echo "${id#sha256:}" | cut -c1-12)
  echo "${var}=${REGISTRY}/${image}:${short}"
  push_cmd="${push_cmd}docker tag $REGISTRY/$image:latest $REGISTRY/$image:$short;docker push -q $REGISTRY/$image:$short >/dev/null;"
done

if [ "$PUSH" = "--push" ]; then
  ssh -o BatchMode=yes "$BUILDER" "$push_cmd"
  echo "pushed content-tagged images to $REGISTRY" >&2
fi
