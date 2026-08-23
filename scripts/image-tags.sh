#!/usr/bin/env bash
# Resolve each locally-built image to a content-addressed tag.
#
# Why this exists: with a mutable `:latest` tag, rebuilding an image does not
# change the Deployment spec, so `helm upgrade` sees nothing to do and the old
# pod keeps running the old code. The deploy looks successful and is not. That
# cost real debugging time twice -- once chasing a "missing" API endpoint that
# was present in the image but not in the running pod.
#
# Tagging by the image's own content digest makes the tag change exactly when
# the image changes: same content, same tag, no pointless restart; new content,
# new tag, guaranteed rollout.
#
# Emits shell assignments for `make deploy` to consume:
#   BACKEND_TAG=meetings-backend:a1b2c3d4e5f6
set -euo pipefail
export PATH="$HOME/.rd/bin:/opt/homebrew/bin:$PATH"

emit() {
  local var="$1" image="$2"
  local id
  id=$(docker image inspect "${image}:latest" --format '{{.Id}}' 2>/dev/null || true)
  if [[ -z "$id" ]]; then
    echo "error: ${image}:latest not found locally -- run 'make images' first" >&2
    exit 1
  fi
  # sha256:abcdef... -> abcdef123456
  echo "${var}=${image}:${id#sha256:}" | cut -c1-$(( ${#var} + ${#image} + 14 ))
}

emit BACKEND_TAG  meetings-backend
emit FRONTEND_TAG meetings-frontend
emit RUNTIME_TAG  meetings-persona-runtime
emit EXEC_TAG     meetings-exec-python
emit CORPUS_TAG   meetings-corpus
