#!/usr/bin/env bash
# Regenerate deploy/charts/meetings/profiles.yaml from the Python definitions.
set -euo pipefail
cd "$(dirname "$0")/../backend"
exec uv run python ../scripts/generate_profile_values.py
