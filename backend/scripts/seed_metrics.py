"""Seed the business-metrics schema.

This is a deliberately *separate* schema with its own read-only role. The DSN
handed to a metrics-capable sandbox can read these tables and nothing else, so a
prompt-injected agent that persuades the model to write hostile SQL still cannot
reach personas, documents, meetings or artifacts.

The numbers describe a fictional manufacturer with a quality problem in one
product line, so a meeting about recalling batch 842 has something real to argue
over rather than inventing figures.
"""

from __future__ import annotations

import asyncio
import random

import structlog
from sqlalchemy import text

from app.core.database import require_session_maker

logger = structlog.get_logger(__name__)

# Fixed seed: demo numbers must be identical on every install, or two people
# running the same scenario see different conclusions.
RNG = random.Random(20260401)

SCHEMA = "metrics"

DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

CREATE TABLE IF NOT EXISTS {SCHEMA}.dim_product (
    product_id   text PRIMARY KEY,
    product_name text NOT NULL,
    line         text NOT NULL
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.dim_region (
    region_id   text PRIMARY KEY,
    region_name text NOT NULL
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.dim_quarter (
    quarter_id text PRIMARY KEY,
    fiscal_year int  NOT NULL,
    quarter     int  NOT NULL
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.fact_revenue (
    quarter_id text NOT NULL REFERENCES {SCHEMA}.dim_quarter(quarter_id),
    product_id text NOT NULL REFERENCES {SCHEMA}.dim_product(product_id),
    region_id  text NOT NULL REFERENCES {SCHEMA}.dim_region(region_id),
    revenue_gbp numeric(14,2) NOT NULL,
    units_sold  int NOT NULL,
    PRIMARY KEY (quarter_id, product_id, region_id)
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.fact_quality (
    quarter_id      text NOT NULL REFERENCES {SCHEMA}.dim_quarter(quarter_id),
    product_id      text NOT NULL REFERENCES {SCHEMA}.dim_product(product_id),
    units_inspected int NOT NULL,
    defects_found   int NOT NULL,
    warranty_claims int NOT NULL,
    PRIMARY KEY (quarter_id, product_id)
);
"""

PRODUCTS = [
    ("P-100", "Aurora Regulator", "Regulators"),
    ("P-200", "Aurora Regulator Pro", "Regulators"),
    ("P-300", "Helios Sensor", "Sensors"),
    ("P-400", "Helios Sensor Mk2", "Sensors"),
]
REGIONS = [("EMEA", "Europe, Middle East & Africa"), ("AMER", "Americas"), ("APAC", "Asia Pacific")]
QUARTERS = [(f"FY25Q{q}", 2025, q) for q in range(1, 5)] + [("FY26Q1", 2026, 1)]

# The product at the centre of the recall discussion.
TROUBLED_PRODUCT = "P-200"


async def seed_metrics() -> None:
    maker = require_session_maker()
    async with maker() as session:
        # asyncpg rejects multiple statements in one execute, so the DDL is
        # split and applied one at a time.
        for statement in (part.strip() for part in DDL.split(";")):
            if statement:
                await session.execute(text(statement))
        await session.commit()

        existing = await session.scalar(text(f"SELECT count(*) FROM {SCHEMA}.fact_revenue"))
        if existing:
            logger.info("seed_metrics_skipped", rows=existing)
            return

        for pid, name, line in PRODUCTS:
            await session.execute(
                text(
                    f"INSERT INTO {SCHEMA}.dim_product VALUES (:i, :n, :l) ON CONFLICT DO NOTHING"
                ),
                {"i": pid, "n": name, "l": line},
            )
        for rid, name in REGIONS:
            await session.execute(
                text(f"INSERT INTO {SCHEMA}.dim_region VALUES (:i, :n) ON CONFLICT DO NOTHING"),
                {"i": rid, "n": name},
            )
        for qid, fy, q in QUARTERS:
            await session.execute(
                text(
                    f"INSERT INTO {SCHEMA}.dim_quarter VALUES (:i, :f, :q) ON CONFLICT DO NOTHING"
                ),
                {"i": qid, "f": fy, "q": q},
            )

        for qid, _fy, _q in QUARTERS:
            for pid, _n, _l in PRODUCTS:
                for rid, _rn in REGIONS:
                    units = RNG.randint(800, 4200)
                    price = RNG.uniform(180, 460)
                    await session.execute(
                        text(
                            f"INSERT INTO {SCHEMA}.fact_revenue VALUES (:q,:p,:r,:rev,:u) "
                            "ON CONFLICT DO NOTHING"
                        ),
                        {"q": qid, "p": pid, "r": rid, "rev": round(units * price, 2), "u": units},
                    )

        for qid, _fy, quarter in QUARTERS:
            for pid, _n, _l in PRODUCTS:
                inspected = RNG.randint(3000, 9000)
                # The troubled line degrades sharply over time; everything else
                # stays flat. That gives the meeting a real signal to find.
                if pid == TROUBLED_PRODUCT:
                    rate = 0.004 + 0.011 * quarter
                else:
                    rate = RNG.uniform(0.002, 0.006)
                defects = int(inspected * rate)
                await session.execute(
                    text(
                        f"INSERT INTO {SCHEMA}.fact_quality VALUES (:q,:p,:i,:d,:w) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {
                        "q": qid,
                        "p": pid,
                        "i": inspected,
                        "d": defects,
                        "w": int(defects * RNG.uniform(0.3, 0.7)),
                    },
                )

        await session.commit()
        logger.info("seed_metrics_complete", products=len(PRODUCTS), quarters=len(QUARTERS))


async def grant_read_only(role: str = "metrics_ro") -> None:
    """Re-assert the read-only grants.

    The CNPG bootstrap creates the role, but tables seeded afterwards need the
    grant applied again -- default privileges only cover objects created by the
    role that set them.
    """
    maker = require_session_maker()
    async with maker() as session:
        await session.execute(text(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role}"))
        await session.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {role}"))
        await session.execute(
            text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT SELECT ON TABLES TO {role}")
        )
        await session.commit()
        logger.info("metrics_grants_applied", role=role)


if __name__ == "__main__":

    async def main() -> None:
        await seed_metrics()
        await grant_read_only()

    asyncio.run(main())
