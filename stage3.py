"""
stage3.py — Stage 3: Integrate

Reads validated rows from stage_cleanup (Stage 2).
Joins clean_product_sales with merchants, customers and bonus_tiers using
SCD-aware point-in-time lookups (valid_from / valid_to on sale_date).
Writes one wide integrated fact table into the stage_integrate schema.

    stages.duckdb
    ├── stage_ingest    ← Stage 1 raw tables
    ├── stage_cleanup   ← Stage 2 clean tables
    └── stage_integrate ← Stage 3 integrated fact table

Full reprocess: drops and rebuilds stage_integrate.integrated_sales on every run.
No date argument — always processes all PASS / WARN rows from stage_cleanup.

Usage:
    python stage3.py

Exit codes:
    0  Integration completed (PARTIAL joins are logged but do not cause failure)
    1  Fatal error (database not found, SQL failure)
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import duckdb

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT_DIR      = Path(__file__).parent
DB_PATH       = ROOT_DIR / "stages.duckdb"
LOG_DIR       = ROOT_DIR / "logs"
SCHEMA        = "stage_integrate"   # output schema
SOURCE_SCHEMA = "stage_cleanup"     # input schema (Stage 2 output)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    run_ts   = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = LOG_DIR / f"stage3_{run_ts}.log"

    logger = logging.getLogger("stage3")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger

# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def initialise_schema(con: duckdb.DuckDBPyConnection, logger: logging.Logger) -> None:
    """Create the stage_integrate schema if it does not already exist."""
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    logger.info("Schema '%s' ready.", SCHEMA)

# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def build_integrated_sales(
    con: duckdb.DuckDBPyConnection,
    logger: logging.Logger,
) -> dict:
    """
    Build stage_integrate.integrated_sales.

    Join strategy — SCD-aware point-in-time:
        For each sale, find the dimension record whose SCD window
        covers the sale_date:
            valid_from <= sale_date AND (valid_to > sale_date OR valid_to IS NULL)

    When multiple versions overlap (e.g. duplicate bonus tier records),
    the version with the latest valid_from is used (ROW_NUMBER trick).

    Source rows: clean_product_sales WHERE validation_status IN ('PASS', 'WARN')
    FAIL rows are excluded.
    Dimension rows with validation_status = 'FAIL' are also excluded from lookups.
    """
    logger.info("Building '%s.integrated_sales' ...", SCHEMA)

    con.execute(f"""
        CREATE OR REPLACE TABLE {SCHEMA}.integrated_sales AS

        WITH

        -- ── 1. Input: validated sales only ───────────────────────────────
        sales AS (
            SELECT *
            FROM   {SOURCE_SCHEMA}.clean_product_sales
            WHERE  validation_status IN ('PASS', 'WARN')
        ),

        -- ── 2. SCD-aware merchant lookup ──────────────────────────────────
        --    For each sale, keep the single merchant-store record whose
        --    SCD window covers sale_date. Latest valid_from wins if several
        --    records overlap.
        merchant_ranked AS (
            SELECT
                s.sale_id,
                m.merchant_id,
                m.merchant_name,
                m.store_name,
                m.country_clean         AS merchant_country,
                m.valid_from            AS merchant_valid_from,
                m.valid_to              AS merchant_valid_to,
                ROW_NUMBER() OVER (
                    PARTITION BY s.sale_id
                    ORDER BY m.valid_from DESC NULLS LAST
                ) AS _rn
            FROM   sales s
            LEFT   JOIN {SOURCE_SCHEMA}.clean_merchants m
                ON  m.store_id    = s.store_id
                AND m.valid_from <= s.sale_date
                AND (m.valid_to   > s.sale_date OR m.valid_to IS NULL)
                AND m.validation_status != 'FAIL'
        ),
        merchant_match AS (
            SELECT
                sale_id, merchant_id, merchant_name, store_name,
                merchant_country, merchant_valid_from, merchant_valid_to
            FROM merchant_ranked
            WHERE _rn = 1
        ),

        -- ── 3. SCD-aware customer lookup ──────────────────────────────────
        customer_ranked AS (
            SELECT
                s.sale_id,
                c.customer_id           AS matched_customer_id,
                c.customer_name,
                c.email                 AS customer_email,
                c.registration_date     AS customer_registration_date,
                c.country_clean         AS customer_country,
                c.valid_from            AS customer_valid_from,
                ROW_NUMBER() OVER (
                    PARTITION BY s.sale_id
                    ORDER BY c.valid_from DESC NULLS LAST
                ) AS _rn
            FROM   sales s
            LEFT   JOIN {SOURCE_SCHEMA}.clean_customers c
                ON  c.customer_id  = s.customer_id
                AND c.valid_from  <= s.sale_date
                AND (c.valid_to    > s.sale_date OR c.valid_to IS NULL)
                AND c.validation_status != 'FAIL'
        ),
        customer_match AS (
            SELECT
                sale_id, matched_customer_id, customer_name, customer_email,
                customer_registration_date, customer_country, customer_valid_from
            FROM customer_ranked
            WHERE _rn = 1
        ),

        -- ── 4. SCD-aware bonus tier lookup ────────────────────────────────
        bonus_ranked AS (
            SELECT
                s.sale_id,
                bt.product_id           AS matched_product_id,
                bt.threshold            AS bonus_threshold,
                bt.bonus_amount,
                bt.valid_from           AS bonus_valid_from,
                bt.valid_to             AS bonus_valid_to,
                ROW_NUMBER() OVER (
                    PARTITION BY s.sale_id
                    ORDER BY bt.valid_from DESC NULLS LAST
                ) AS _rn
            FROM   sales s
            LEFT   JOIN {SOURCE_SCHEMA}.clean_bonus_tiers bt
                ON  bt.product_id  = s.product_id
                AND bt.valid_from <= s.sale_date
                AND (bt.valid_to   > s.sale_date OR bt.valid_to IS NULL)
                AND bt.validation_status != 'FAIL'
        ),
        bonus_match AS (
            SELECT
                sale_id, matched_product_id, bonus_threshold,
                bonus_amount, bonus_valid_from, bonus_valid_to
            FROM bonus_ranked
            WHERE _rn = 1
        )

        -- ── 5. Final wide fact table ──────────────────────────────────────
        SELECT
            -- Sale identifiers
            s.sale_id,
            s.partition_date,
            s.sale_date,

            -- Store / merchant
            s.store_id,
            mm.merchant_id,
            mm.merchant_name,
            mm.store_name,
            mm.merchant_country,
            mm.merchant_valid_from,
            mm.merchant_valid_to,

            -- Product
            s.product_id,
            s.product_name,
            s.quantity,
            s.unit_price,
            s.total_amount,

            -- Customer
            s.customer_id,
            cm.customer_name,
            cm.customer_email,
            cm.customer_registration_date,
            cm.customer_country,
            cm.customer_valid_from,

            -- Bonus tier active on the sale date
            bm.bonus_threshold,
            bm.bonus_amount,
            bm.bonus_valid_from,
            bm.bonus_valid_to,

            -- Sale geography (cleaned)
            s.country               AS sale_country_raw,
            s.country_clean         AS sale_country,

            -- Validation provenance from Stage 2
            s.validation_status     AS sale_validation_status,
            s.validation_reasons    AS sale_validation_reasons,

            -- Join completeness
            CASE
                WHEN mm.merchant_id        IS NULL
                  OR cm.matched_customer_id IS NULL
                  OR bm.matched_product_id  IS NULL
                THEN 'PARTIAL'
                ELSE 'FULL'
            END AS join_status,

            NULLIF(CONCAT_WS('|',
                CASE WHEN mm.merchant_id        IS NULL THEN 'merchant_not_found'   END,
                CASE WHEN cm.matched_customer_id IS NULL THEN 'customer_not_found'  END,
                CASE WHEN bm.matched_product_id  IS NULL THEN 'bonus_tier_not_found' END
            ), '') AS unmatched_dimensions,

            -- Pipeline timestamp
            CURRENT_TIMESTAMP AS integrated_at

        FROM      sales         s
        LEFT JOIN merchant_match mm ON mm.sale_id = s.sale_id
        LEFT JOIN customer_match cm ON cm.sale_id = s.sale_id
        LEFT JOIN bonus_match    bm ON bm.sale_id = s.sale_id
    """)

    # ── Stats ──────────────────────────────────────────────────────────────
    total = con.execute(
        f"SELECT COUNT(*) FROM {SCHEMA}.integrated_sales"
    ).fetchone()[0]

    status_rows = con.execute(f"""
        SELECT join_status, COUNT(*)
        FROM   {SCHEMA}.integrated_sales
        GROUP  BY join_status
    """).fetchall()
    counts = {s: c for s, c in status_rows}

    logger.info(
        "  integrated_sales: %d rows  |  FULL=%d  PARTIAL=%d",
        total,
        counts.get("FULL", 0),
        counts.get("PARTIAL", 0),
    )

    if counts.get("PARTIAL", 0):
        partial_sample = con.execute(f"""
            SELECT sale_id, unmatched_dimensions
            FROM   {SCHEMA}.integrated_sales
            WHERE  join_status = 'PARTIAL'
            LIMIT  5
        """).fetchall()
        for sale_id, dims in partial_sample:
            logger.warning("  PARTIAL join  sale_id=%s  missing=%s", sale_id, dims)

    return {"total": total, **counts}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger = setup_logging()

    logger.info("=" * 60)
    logger.info("Stage 3 started  (full reprocess)")
    logger.info("Database          %s", DB_PATH)
    logger.info("Source schema     %s", SOURCE_SCHEMA)
    logger.info("Output schema     %s", SCHEMA)
    logger.info("=" * 60)

    if not DB_PATH.exists():
        logger.error("Database not found: %s  — run Stage 1 and Stage 2 first.", DB_PATH)
        sys.exit(1)

    con = duckdb.connect(str(DB_PATH))

    try:
        initialise_schema(con, logger)
        result = build_integrated_sales(con, logger)
    except Exception as exc:  # noqa: BLE001
        logger.error("Stage 3 failed: %s", exc)
        con.close()
        sys.exit(1)

    con.close()

    logger.info("")
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("  Table      : %s.integrated_sales", SCHEMA)
    logger.info("  Total rows : %d", result["total"])
    logger.info("  FULL joins : %d", result.get("FULL", 0))
    logger.info("  PARTIAL    : %d", result.get("PARTIAL", 0))
    logger.info("=" * 60)
    logger.info("Stage 3 complete.")


if __name__ == "__main__":
    main()
