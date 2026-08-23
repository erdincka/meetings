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
for module in protocol.py recovery.py prompts.py; do
  cp "$src/$module" "$here/runtime/$module"
  echo "vendored $module"
done

# The tool guidance table lives inside profiles.py, which imports application
# code the sandbox image does not carry. Extract just the table, resolving the
# tool-name constants to literals so the copy stands alone.
python3 - "$src/profiles.py" "$here/runtime/tool_guidance.py" <<'PYEOF'
import re, sys
src, dest = sys.argv[1], sys.argv[2]
text = open(src).read()
start = text.index("# What each tool is for, in the words the agent sees.")
end = text.index("@dataclass(frozen=True)")
block = text[start:end].rstrip() + "\n"
for name, value in re.findall(r'^([A-Z_]+) = "([a-z_]+)"$', text, re.M):
    block = re.sub(rf"\b{name}\b(?=:)", f'"{value}"', block)
open(dest, "w").write(
    '"""Tool guidance shown to agents.\n\n'
    "Vendored from backend/app/orchestration/profiles.py by sync-shared.sh so the\n"
    'sandbox image carries no dependency on the backend package. CI diffs the copies.\n"""\n\n'
    "from __future__ import annotations\n\n" + block
)
PYEOF
echo "vendored tool_guidance.py"
