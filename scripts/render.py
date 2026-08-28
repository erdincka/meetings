"""Render a ${VAR} template against deploy/cluster/cluster.env.

A deliberately small substitute for envsubst, which is not installed on macOS by
default. Unknown variables are an error rather than being silently replaced with
an empty string -- a NetworkPolicy CIDR or a registry address that quietly
became "" is far harder to diagnose than a failed render.

Two forms beyond a bare ${VAR}, neither of which envsubst has -- which is most
of why this exists rather than a one-line shell alias:

`${VAR:list}` expands a comma- or space-separated setting into a YAML flow
sequence. A setting documented as taking several values, interpolated into a
single `- "${VAR}"`, produces one item containing a comma -- and an ipBlock CIDR
of "10.0.0.1/32,10.0.0.2/32" is rejected far from the file that caused it.

`${VAR:-default}` makes a setting optional. Without it, every new template
variable is a breaking change for everyone whose cluster.env predates it: the
render aborts on a name they have never heard of, over a feature they did not
ask for. Required settings stay required -- omitting the default is what marks
them so.

    scripts/render.py deploy/cluster/templates/metallb-pool.yaml.tmpl
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / "deploy" / "cluster" / "cluster.env"
EXAMPLE = REPO / "deploy" / "cluster" / "cluster.env.example"
PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)(:list)?(?::-([^}]*))?\}")


def load_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        sys.exit(
            f"{ENV_FILE.relative_to(REPO)} not found.\n"
            f"Copy {EXAMPLE.relative_to(REPO)} to it and edit it for your cluster."
        )
    env: dict[str, str] = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    # Real environment wins, so a one-off override needs no file edit.
    env.update({k: v for k, v in os.environ.items() if k in env})
    return env


def render(template: Path, env: dict[str, str]) -> str:
    missing: set[str] = set()

    def substitute(match: re.Match[str]) -> str:
        name, modifier, fallback = match.group(1), match.group(2), match.group(3)
        if name in env:
            value = env[name]
        elif fallback is not None:
            value = fallback
        else:
            missing.add(name)
            return match.group(0)
        if modifier == ":list":
            # json.dumps, not hand-rolled quoting: a YAML flow sequence of
            # double-quoted scalars is valid JSON, and json escapes correctly.
            return json.dumps(re.split(r"[,\s]+", value.strip()) if value.strip() else [])
        return value

    out = PLACEHOLDER.sub(substitute, template.read_text())
    if missing:
        sys.exit(f"{template.name}: undefined in cluster.env: {', '.join(sorted(missing))}")
    return out


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit("usage: render.py <template> [more templates...]")
    env = load_env()
    for arg in sys.argv[1:]:
        template = Path(arg)
        target = template.with_suffix("")  # strip .tmpl
        target.write_text(render(template, env))
        # Paths may be given relative or absolute; show whichever is shorter
        # rather than assuming one form.
        try:
            shown = target.resolve().relative_to(REPO)
        except ValueError:
            shown = target
        print(f"  rendered {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
