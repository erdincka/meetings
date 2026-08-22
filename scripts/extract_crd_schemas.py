"""Extract JSON schemas for third-party CRDs so kubeconform can validate them.

kubeconform resolves schemas from the datree CRDs-catalog, which does not carry
agents.x-k8s.io. Without these files SandboxTemplate and SandboxWarmPool are
silently *skipped* -- so a typo in the resources this whole project depends on
would sail through CI looking green.

Regenerate after upgrading Agent Sandbox:
    scripts/extract-crd-schemas.sh
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

GROUP_SUFFIX = "agents.x-k8s.io"


def main(out_dir: str, context: str) -> int:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    listing = subprocess.run(
        ["kubectl", "--context", context, "get", "crd", "-o", "name"],
        capture_output=True,
        text=True,
        check=True,
    )
    names = [n for n in listing.stdout.split() if GROUP_SUFFIX in n]
    if not names:
        print(f"no {GROUP_SUFFIX} CRDs found in context {context}", file=sys.stderr)
        return 1

    written = 0
    for name in names:
        raw = subprocess.run(
            ["kubectl", "--context", context, "get", name, "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        crd = json.loads(raw)
        group = crd["spec"]["group"]
        kind = crd["spec"]["names"]["kind"]

        for version in crd["spec"]["versions"]:
            schema = (version.get("schema") or {}).get("openAPIV3Schema")
            if not schema:
                continue
            # Must match the -schema-location template exactly:
            #   {{ .ResourceKind }}-{{ .Group }}-{{ .ResourceAPIVersion }}.json
            # kubeconform lowercases ResourceKind, so lowercase the whole name.
            filename = f"{kind}-{group}-{version['name']}.json".lower()
            (out / filename).write_text(json.dumps(schema, indent=2) + "\n")
            print(f"  wrote {filename}")
            written += 1

    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "deploy/schemas",
                          sys.argv[2] if len(sys.argv) > 2 else "kind-meetings"))
