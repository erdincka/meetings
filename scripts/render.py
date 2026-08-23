"""Render a ${VAR} template against deploy/k3s/lab.env.

A deliberately small substitute for envsubst, which is not installed on macOS by
default. Unknown variables are an error rather than being silently replaced with
an empty string -- a NetworkPolicy CIDR or a registry address that quietly
became "" is far harder to diagnose than a failed render.

    scripts/render.py deploy/k3s/metallb-pool.yaml.tmpl
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / "deploy" / "k3s" / "lab.env"
EXAMPLE = REPO / "deploy" / "k3s" / "lab.env.example"
PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


def load_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        sys.exit(
            f"{ENV_FILE.relative_to(REPO)} not found.\n"
            f"Copy {EXAMPLE.relative_to(REPO)} to it and edit for your network."
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
        name = match.group(1)
        if name not in env:
            missing.add(name)
            return match.group(0)
        return env[name]

    out = PLACEHOLDER.sub(substitute, template.read_text())
    if missing:
        sys.exit(
            f"{template.name}: undefined in lab.env: {', '.join(sorted(missing))}"
        )
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
