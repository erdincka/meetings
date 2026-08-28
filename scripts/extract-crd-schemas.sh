#!/usr/bin/env bash
# Thin wrapper; see scripts/extract_crd_schemas.py for what and why.
set -euo pipefail
exec python3 "$(dirname "$0")/extract_crd_schemas.py" "${1:-deploy/schemas}" "${KCTX:-}"
