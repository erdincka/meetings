#!/usr/bin/env bash
# Vendor the modules shared with the backend into this image's source tree.
#
# protocol.py is the wire contract and recovery.py parses model output; both are
# authored under backend/app/orchestration and copied here so the sandbox image
# has no dependency on the backend package. `make check` verifies the copies are
# identical, so drift fails CI rather than surfacing as a confusing runtime bug.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
src="$here/../../backend/app/orchestration"
for module in protocol.py recovery.py; do
  cp "$src/$module" "$here/runtime/$module"
  echo "vendored $module"
done
