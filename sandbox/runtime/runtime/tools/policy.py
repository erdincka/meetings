"""Deterministic policy and compliance checks.

Deliberately rule-based rather than model-based. An LLM asked whether text is
compliant will sometimes say yes because it reads as reassuring, and a
compliance check that can hallucinate a pass is worse than none at all. These
rules either match or they do not, and the agent is told exactly which fired.

The packs are data, so adding a rule needs no code change.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog
import yaml
from langchain_core.tools import StructuredTool

logger = structlog.get_logger(__name__)

PACK_DIR = Path(__file__).resolve().parent.parent / "policy_packs"


def _load_pack(name: str) -> list[dict[str, Any]]:
    path = PACK_DIR / f"{name}.yaml"
    if not path.exists():
        return []
    loaded = yaml.safe_load(path.read_text()) or {}
    rules: list[dict[str, Any]] = loaded.get("rules", [])
    return rules


def available_packs() -> list[str]:
    return sorted(p.stem for p in PACK_DIR.glob("*.yaml"))


def evaluate(text: str, pack: str) -> list[dict[str, str]]:
    """Return every rule that fires against ``text``. Pure, so it is testable."""
    findings: list[dict[str, str]] = []
    for rule in _load_pack(pack):
        pattern = rule.get("pattern")
        if not pattern:
            continue
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            findings.append(
                {
                    "rule_id": str(rule.get("id", "?")),
                    "severity": str(rule.get("severity", "medium")),
                    "message": str(rule.get("message", "")),
                    "remediation": str(rule.get("remediation", "")),
                    "matched": match.group(0)[:120],
                }
            )
    return findings


def build_policy_tool(**_ignored: Any) -> StructuredTool:
    async def check_policy_compliance(text: str, pack: str = "gdpr") -> str:
        """Check a draft against a compliance rule pack before it is circulated.

        Packs: gdpr, sox, export-control, brand. Rules are deterministic -- a
        clean result means no rule matched, not that a model judged it fine.
        """
        packs = available_packs()
        if pack not in packs:
            return f"Unknown pack '{pack}'. Available: {', '.join(packs)}."

        findings = evaluate(text, pack)
        if not findings:
            return f"No {pack} rules matched. This is a rule check, not legal advice."

        lines = [f"{len(findings)} {pack} finding(s):"]
        for f in findings:
            lines.append(
                f"- [{f['severity'].upper()} {f['rule_id']}] {f['message']}\n"
                f'    matched: "{f["matched"]}"\n'
                f"    fix: {f['remediation']}"
            )
        return "\n".join(lines)

    return StructuredTool.from_function(
        coroutine=check_policy_compliance,
        name="check_policy_compliance",
        description=check_policy_compliance.__doc__ or "",
    )
