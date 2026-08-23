"""Read-only SQL against the business-metrics schema.

The DSN is mounted from a Secret that only metrics-capable SandboxTemplates
reference, and it authenticates as a role that can read one schema and write
nothing. Statement shape is checked here as well -- not because the database
needs the help, but because a clear "SELECT only" message teaches the model what
to do next, where a permission error just reads as a fault.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import structlog
from langchain_core.tools import StructuredTool

logger = structlog.get_logger(__name__)

DSN_FILE = Path(os.getenv("METRICS_DSN_FILE", "/etc/sandbox/secrets/metrics-dsn"))
MAX_ROWS = 500
STATEMENT_TIMEOUT_MS = 5000

_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do)\b",
    re.IGNORECASE,
)


def _looks_read_only(sql: str) -> tuple[bool, str]:
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        return False, "Empty query."
    # Multiple statements would let a write ride along behind a SELECT.
    if ";" in stripped:
        return False, "Submit a single statement."
    if not re.match(r"^(select|with)\b", stripped, re.IGNORECASE):
        return False, "Only SELECT (or WITH ... SELECT) queries are permitted."
    if _WRITE_KEYWORDS.search(stripped):
        return False, "Only read queries are permitted."
    return True, ""


def build_metrics_tool(**_ignored: Any) -> StructuredTool:
    async def query_business_metrics(sql: str) -> str:
        """Query the company's business metrics with read-only SQL.

        Schema `metrics`: dim_product(product_id, product_name, line),
        dim_region(region_id, region_name),
        dim_quarter(quarter_id, fiscal_year, quarter),
        fact_revenue(quarter_id, product_id, region_id, revenue_gbp, units_sold),
        fact_quality(quarter_id, product_id, units_inspected, defects_found,
        warranty_claims).

        SELECT only. Results are capped, so aggregate rather than dumping rows.
        """
        ok, reason = _looks_read_only(sql)
        if not ok:
            return f"Query rejected: {reason}"

        if not DSN_FILE.exists():
            return (
                "No metrics credential is mounted in this sandbox, so this "
                "persona cannot query business metrics."
            )

        import asyncpg

        try:
            conn = await asyncpg.connect(DSN_FILE.read_text().strip(), timeout=10)
        except Exception as exc:
            logger.error("metrics_connect_failed", error=str(exc))
            return f"Could not reach the metrics database: {exc}"

        try:
            await conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            rows = await conn.fetch(f"SELECT * FROM ({sql.rstrip(';')}) q LIMIT {MAX_ROWS}")
        except Exception as exc:
            logger.warning("metrics_query_failed", error=str(exc))
            return f"Query failed: {exc}"
        finally:
            await conn.close()

        if not rows:
            return "The query returned no rows."

        headers = list(rows[0].keys())
        lines = [" | ".join(headers), "-" * 40]
        lines.extend(" | ".join(str(r[h]) for h in headers) for r in rows)
        if len(rows) == MAX_ROWS:
            lines.append(f"(truncated at {MAX_ROWS} rows -- aggregate instead)")
        return "\n".join(lines)

    return StructuredTool.from_function(
        coroutine=query_business_metrics,
        name="query_business_metrics",
        description=query_business_metrics.__doc__ or "",
    )
