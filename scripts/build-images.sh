#!/usr/bin/env bash
# Build the five images and push them to the registry in cluster.env.
#
# Two modes, chosen by whether BUILDER is set:
#
#   BUILDER empty   build locally with the Docker daemon on this machine
#   BUILDER set     rsync the tree to that SSH target and build there
#
# The remote mode exists for the case where the workstation and the nodes differ
# in architecture. Cross-building Python and Node images under emulation is slow
# enough to hurt an inner loop, and a native build on a machine that matches the
# nodes is the cheapest fix. It is an optimisation, not a requirement — the
# local path produces identical images.
#
# For a published release, do not run this at all: pull the signed images and
# verify them with `make verify-images`.
set -euo pipefail
export PATH="$HOME/.rd/bin:/opt/homebrew/bin:$PATH"

REPO=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE="$REPO/deploy/cluster/cluster.env"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

REGISTRY=${IMAGE_REGISTRY:?set IMAGE_REGISTRY in deploy/cluster/cluster.env}
# shellcheck source=scripts/builder-target.sh
. "$REPO/scripts/builder-target.sh"
BUILDER=$(normalize_builder "${BUILDER:-}")
BUILDER_DIR=${BUILDER_DIR:-/home/ubuntu/meetings}

# name:context:target -- an empty target means the Dockerfile has no stages to
# select between.
IMAGES="meetings-backend:backend:runtime
meetings-frontend:frontend:runtime
meetings-persona-runtime:sandbox/runtime:runtime
meetings-exec-python:sandbox/exec-python:
meetings-corpus:sandbox/corpus:"

build_script() {
  local cmds=""
  for entry in $IMAGES; do
    local name context target
    name=${entry%%:*}
    context=$(echo "$entry" | cut -d: -f2)
    target=$(echo "$entry" | cut -d: -f3)
    local flags=""
    [ -n "$target" ] && flags="--target $target"
    cmds="${cmds}docker build -q $flags -t $REGISTRY/$name:latest $context && "
  done
  echo "${cmds}true"
}

if [ -z "$BUILDER" ]; then
  echo ">> building locally"
  ( cd "$REPO" && eval "$(build_script)" ) >/dev/null
else
  echo ">> building on $BUILDER"
  rsync -az --delete \
    --exclude '.git' --exclude '**/node_modules' --exclude '**/.venv' \
    --exclude '**/__pycache__' --exclude '.local-assets' --exclude '**/.next' \
    "$REPO/" "$BUILDER:$BUILDER_DIR/"
  ssh -o BatchMode=yes "$BUILDER" "cd $BUILDER_DIR && $(build_script)" >/dev/null
fi

bash "$REPO/scripts/image-tags.sh" --push
