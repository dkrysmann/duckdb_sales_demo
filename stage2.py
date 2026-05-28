"""
stage2.py — Stage 2: Data Cleaning and Validation

Reads raw tables from the default schema of stage_1.duckdb (written by Stage 1).
Writes cleaned tables into the `stage_2` schema of the same database.

    stages.duckdb
    ├── stage_ingest schema   ← Stage 1 raw tables (product_sales, merchants, …)
    └── stage_cleanup schema  ← Stage 2 clean tables (clean_product_sales, …)

Full reprocess: every run drops and rebuilds all stage_2.clean_* tables from scratch.

Usage:
    python stage2.py [--threshold 80]

Exit codes:
    0  All rows PASS or WARN only
    1  One or more rows have validation_status = FAIL
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pycountry
from rapidfuzz import fuzz, process as fz_process, utils as fz_utils

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT_DIR          = Path(__file__).parent
DB_PATH           = ROOT_DIR / "stages.duckdb"
LOG_DIR           = ROOT_DIR / "logs"
DEFAULT_THRESHOLD = 80   # minimum fuzzy match score 0–100 for country acceptance
SCHEMA            = "stage_cleanup"   # DuckDB schema for all clean_* output tables
SOURCE_SCHEMA     = "stage_ingest"    # DuckDB schema where Stage 1 raw tables live

# Pre-build the canonical country name list once at import time
_COUNTRY_NAMES: list[str] = [c.name for c in pycountry.countries]

# Common abbreviations and alpha-2/alpha-3 codes not caught by fuzzy matching.
# Maps lowercase stripped value → canonical pycountry name.
_COUNTRY_ALIASES: dict[str, str] = {
    "uk":  "United Kingdom",
    "gb":  "United Kingdom",
    "usa": "United States",
    "us":  "United States",
    "uae": "United Arab Emirates",
    "eu":  "European Union",  # not in pycountry but harmless to map
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    run_ts   = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = LOG_DIR / f"stage2_{run_ts}.log"

    logger = logging.getLogger("stage2")
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
# Country normalisation
# ---------------------------------------------------------------------------

def _strip_non_alpha(value: str) -> str:
    """Replace non-alphanumeric characters (except spaces) with a space,
    then collapse multiple spaces."""
    cleaned = re.sub(r"[^a-zA-Z0-9 ]", " ", value)
    return re.sub(r"\s+", " ", cleaned).strip()


def build_country_lookup(
    con: duckdb.DuckDBPyConnection,
    source_table: str,
    col: str,
    threshold: int,
    logger: logging.Logger,
) -> list[tuple[str, str | None, int]]:
    """
    Fetch every unique non-NULL value of `col` from `source_table`.
    For each value:
      1. Strip non-alphanumeric characters.
      2. Fuzzy-match the result against the pycountry ISO country name list
         (using WRatio scorer for best tolerance of word order / abbreviations).
      3. If score >= threshold  → canonical name is used.
         If score <  threshold  → country_clean = NULL, row will receive WARN.

    Returns list of (dirty_value, canonical_name | None, score).
    """
    dirty_values: list[str] = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT {col} FROM {source_table} WHERE {col} IS NOT NULL"
        ).fetchall()
    ]

    results: list[tuple[str, str | None, int]] = []
    for dirty in dirty_values:
        normalized = _strip_non_alpha(dirty)

        if not normalized:
            logger.warning("[%s] '%s' is empty after stripping — cannot match.", source_table, dirty)
            results.append((dirty, None, 0))
            continue

        # Check abbreviation/alias dict first (e.g. "U.K." strips to "U K" → collapse → "uk")
        alias = _COUNTRY_ALIASES.get(re.sub(r"\s+", "", normalized).lower())
        if alias:
            logger.debug("[%s] '%s' → '%s' (alias lookup)", source_table, dirty, alias)
            results.append((dirty, alias, 100))
            continue

        # Fuzzy match — processor=default_process handles case and extra whitespace
        match = fz_process.extractOne(
            normalized, _COUNTRY_NAMES,
            scorer=fuzz.WRatio,
            processor=fz_utils.default_process,
        )
        if match and match[1] >= threshold:
            logger.debug("[%s] '%s' → '%s' (score=%d)", source_table, dirty, match[0], int(match[1]))
            results.append((dirty, match[0], int(match[1])))
        else:
            score = int(match[1]) if match else 0
            logger.warning(
                "[%s] '%s' unmatched — best score %d < threshold %d.",
                source_table, dirty, score, threshold,
            )
            results.append((dirty, None, score))

    return results


def register_lookup_table(
    con: duckdb.DuckDBPyConnection,
    tmp_name: str,
    rows: list[tuple],
) -> None:
    """Store the lookup result as a named TEMP table for use in SQL joins."""
    con.execute(f"DROP TABLE IF EXISTS {tmp_name}")
    con.execute(f"""
        CREATE TEMP TABLE {tmp_name} (
            dirty_value          VARCHAR,
            country_clean        VARCHAR,
            country_match_score  INTEGER
        )
    """)
    if rows:
        con.executemany(f"INSERT INTO {tmp_name} VALUES (?, ?, ?)", rows)

# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def initialise_schema(con: duckdb.DuckDBPyConnection, logger: logging.Logger) -> None:
    """Create the stage_2 schema if it does not already exist.
    Also removes any stale clean_* tables that were written into the main schema
    by earlier versions of this script (safe no-op if they don't exist).
    """
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    logger.info("Schema '%s' ready.", SCHEMA)

    # Remove stale clean_* tables that may exist in the old stage_2 schema
    # (written by an earlier version of this script).
    _LEGACY_TABLES = (
        "clean_product_sales", "clean_merchants",
        "clean_customers",     "clean_bonus_tiers",
    )
    for tbl in _LEGACY_TABLES:
        con.execute(f"DROP TABLE IF EXISTS stage_2.{tbl}")
        con.execute(f"DROP TABLE IF EXISTS main.{tbl}")
    logger.debug("Legacy clean_* tables from old schemas removed (if any existed).")

# ---------------------------------------------------------------------------
# Shared helper — count statuses after building a clean table
# ---------------------------------------------------------------------------

def _count_statuses(
    con: duckdb.DuckDBPyConnection,
    table: str,
    logger: logging.Logger,
) -> dict:
    total = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    counts = {
        s: c
        for s, c in con.execute(
            f"SELECT validation_status, COUNT(*) FROM {table} GROUP BY validation_status"
        ).fetchall()
    }
    logger.info(
        "  %-30s %d rows  |  PASS=%d  WARN=%d  FAIL=%d",
        table, total,
        counts.get("PASS", 0), counts.get("WARN", 0), counts.get("FAIL", 0),
    )
    return {"table": table, "total": total, **counts}

# ---------------------------------------------------------------------------
# clean_product_sales
# ---------------------------------------------------------------------------

def clean_product_sales(
    con: duckdb.DuckDBPyConnection,
    threshold: int,
    logger: logging.Logger,
) -> dict:
    """
    Validations applied
    ───────────────────
    FAIL  NULL_sale_id              sale_id IS NULL
    FAIL  NULL_store_id             store_id IS NULL
    FAIL  NULL_product_id           product_id IS NULL
    FAIL  NULL_customer_id          customer_id IS NULL
    FAIL  NULL_sale_date            sale_date IS NULL
    FAIL  INVALID_unit_price        unit_price <= 0
    FAIL  FK_store_not_found        store_id not active in merchants on sale_date (SCD-aware)
    FAIL  FK_customer_not_found     customer_id not active in customers on sale_date (SCD-aware)
    WARN  LOW_COUNTRY_MATCH_SCORE   fuzzy score < threshold

    Country columns added: country_clean, country_match_score
    """
    logger.info("Cleaning 'product_sales' ...")

    lookup = build_country_lookup(con, f"{SOURCE_SCHEMA}.product_sales", "country", threshold, logger)
    register_lookup_table(con, "_cl_ps", lookup)

    con.execute(f"""
        CREATE OR REPLACE TABLE {SCHEMA}.clean_product_sales AS
        WITH base AS (
            SELECT
                ps.*,
                cl.country_clean,
                cl.country_match_score
            FROM {SOURCE_SCHEMA}.product_sales ps
            LEFT JOIN _cl_ps cl ON ps.country = cl.dirty_value
        ),
        with_fk AS (
            SELECT
                b.*,
                -- SCD-aware: was this store active on the sale date?
                (
                    SELECT 1 FROM {SOURCE_SCHEMA}.merchants m
                    WHERE  m.store_id   = b.store_id
                      AND  m.valid_from <= b.sale_date
                      AND  (m.valid_to  >  b.sale_date OR m.valid_to IS NULL)
                    LIMIT 1
                ) AS _store_found,
                -- SCD-aware: was this customer registered by the sale date?
                (
                    SELECT 1 FROM {SOURCE_SCHEMA}.customers c
                    WHERE  c.customer_id = b.customer_id
                      AND  c.valid_from <= b.sale_date
                      AND  (c.valid_to  >  b.sale_date OR c.valid_to IS NULL)
                    LIMIT 1
                ) AS _cust_found
            FROM base b
        )
        SELECT
            -- ── Source columns ─────────────────────────────────────────────
            sale_id, store_id, product_id, product_name, customer_id,
            quantity, unit_price, total_amount, sale_date, country,
            partition_date, ingested_at,
            -- ── Country normalisation ───────────────────────────────────────
            country_clean,
            country_match_score,
            -- ── Pipeline timestamp ──────────────────────────────────────────
            CURRENT_TIMESTAMP AS cleaned_at,
            -- ── Validation reasons (NULL when all checks pass) ──────────────
            NULLIF(CONCAT_WS('|',
                CASE WHEN sale_id     IS NULL                              THEN 'NULL_sale_id'            END,
                CASE WHEN store_id    IS NULL                              THEN 'NULL_store_id'           END,
                CASE WHEN product_id  IS NULL                              THEN 'NULL_product_id'         END,
                CASE WHEN customer_id IS NULL                              THEN 'NULL_customer_id'        END,
                CASE WHEN sale_date   IS NULL                              THEN 'NULL_sale_date'          END,
                CASE WHEN unit_price IS NOT NULL AND unit_price <= 0       THEN 'INVALID_unit_price'      END,
                CASE WHEN store_id   IS NOT NULL AND _store_found IS NULL  THEN 'FK_store_not_found'      END,
                CASE WHEN customer_id IS NOT NULL AND _cust_found IS NULL  THEN 'FK_customer_not_found'   END,
                CASE WHEN country_match_score IS NOT NULL
                      AND country_match_score < {threshold}                THEN 'LOW_COUNTRY_MATCH_SCORE' END
            ), '') AS validation_reasons,
            -- ── Validation status ───────────────────────────────────────────
            CASE
                WHEN (
                    (sale_id     IS NULL)::INT +
                    (store_id    IS NULL)::INT +
                    (product_id  IS NULL)::INT +
                    (customer_id IS NULL)::INT +
                    (sale_date   IS NULL)::INT +
                    (unit_price IS NOT NULL AND unit_price  <= 0)::INT +
                    (store_id   IS NOT NULL AND _store_found IS NULL)::INT +
                    (customer_id IS NOT NULL AND _cust_found IS NULL)::INT
                ) > 0 THEN 'FAIL'
                WHEN country_match_score IS NOT NULL
                 AND country_match_score < {threshold} THEN 'WARN'
                ELSE 'PASS'
            END AS validation_status
        FROM with_fk
    """)

    return _count_statuses(con, f"{SCHEMA}.clean_product_sales", logger)

# ---------------------------------------------------------------------------
# clean_merchants
# ---------------------------------------------------------------------------

def clean_merchants(
    con: duckdb.DuckDBPyConnection,
    threshold: int,
    logger: logging.Logger,
) -> dict:
    """
    Validations applied
    ───────────────────
    FAIL  NULL_merchant_id          merchant_id IS NULL
    FAIL  NULL_store_id             store_id IS NULL
    WARN  LOW_COUNTRY_MATCH_SCORE   fuzzy score < threshold

    Country columns added: country_clean, country_match_score
    """
    logger.info("Cleaning 'merchants' ...")

    lookup = build_country_lookup(con, f"{SOURCE_SCHEMA}.merchants", "country", threshold, logger)
    register_lookup_table(con, "_cl_m", lookup)

    con.execute(f"""
        CREATE OR REPLACE TABLE {SCHEMA}.clean_merchants AS
        SELECT
            -- ── Source columns ─────────────────────────────────────────────
            m.merchant_id, m.merchant_name, m.store_id, m.store_name, m.country,
            m.valid_from,  m.valid_to,      m.is_current,
            -- ── Country normalisation ───────────────────────────────────────
            cl.country_clean,
            cl.country_match_score,
            -- ── Pipeline timestamp ──────────────────────────────────────────
            CURRENT_TIMESTAMP AS cleaned_at,
            -- ── Validation reasons ──────────────────────────────────────────
            NULLIF(CONCAT_WS('|',
                CASE WHEN m.merchant_id IS NULL THEN 'NULL_merchant_id' END,
                CASE WHEN m.store_id    IS NULL THEN 'NULL_store_id'    END,
                CASE WHEN cl.country_match_score IS NOT NULL
                      AND cl.country_match_score < {threshold}
                     THEN 'LOW_COUNTRY_MATCH_SCORE' END
            ), '') AS validation_reasons,
            -- ── Validation status ───────────────────────────────────────────
            CASE
                WHEN m.merchant_id IS NULL OR m.store_id IS NULL  THEN 'FAIL'
                WHEN cl.country_match_score IS NOT NULL
                 AND cl.country_match_score < {threshold}          THEN 'WARN'
                ELSE 'PASS'
            END AS validation_status
        FROM {SOURCE_SCHEMA}.merchants m
        LEFT JOIN _cl_m cl ON m.country = cl.dirty_value
    """)

    return _count_statuses(con, f"{SCHEMA}.clean_merchants", logger)

# ---------------------------------------------------------------------------
# clean_customers
# ---------------------------------------------------------------------------

def clean_customers(
    con: duckdb.DuckDBPyConnection,
    threshold: int,
    logger: logging.Logger,
) -> dict:
    """
    Validations applied
    ───────────────────
    FAIL  NULL_customer_id           customer_id IS NULL
    FAIL  NULL_registration_date     registration_date IS NULL
    WARN  INVALID_EMAIL_FORMAT       email does not match pattern *@*.*
    WARN  LOW_COUNTRY_MATCH_SCORE    fuzzy score < threshold

    Country columns added: country_clean, country_match_score
    """
    logger.info("Cleaning 'customers' ...")

    lookup = build_country_lookup(con, f"{SOURCE_SCHEMA}.customers", "country", threshold, logger)
    register_lookup_table(con, "_cl_c", lookup)

    con.execute(f"""
        CREATE OR REPLACE TABLE {SCHEMA}.clean_customers AS
        SELECT
            -- ── Source columns ─────────────────────────────────────────────
            c.customer_id, c.customer_name, c.email, c.registration_date, c.country,
            c.valid_from,  c.valid_to,      c.is_current,
            -- ── Country normalisation ───────────────────────────────────────
            cl.country_clean,
            cl.country_match_score,
            -- ── Pipeline timestamp ──────────────────────────────────────────
            CURRENT_TIMESTAMP AS cleaned_at,
            -- ── Validation reasons ──────────────────────────────────────────
            NULLIF(CONCAT_WS('|',
                CASE WHEN c.customer_id       IS NULL THEN 'NULL_customer_id'       END,
                CASE WHEN c.registration_date IS NULL THEN 'NULL_registration_date' END,
                CASE WHEN c.email IS NOT NULL
                      AND c.email NOT LIKE '%@%.%'   THEN 'INVALID_EMAIL_FORMAT'    END,
                CASE WHEN cl.country_match_score IS NOT NULL
                      AND cl.country_match_score < {threshold}
                     THEN 'LOW_COUNTRY_MATCH_SCORE' END
            ), '') AS validation_reasons,
            -- ── Validation status ───────────────────────────────────────────
            CASE
                WHEN c.customer_id IS NULL OR c.registration_date IS NULL THEN 'FAIL'
                WHEN (c.email IS NOT NULL AND c.email NOT LIKE '%@%.%')
                  OR (cl.country_match_score IS NOT NULL
                      AND cl.country_match_score < {threshold})               THEN 'WARN'
                ELSE 'PASS'
            END AS validation_status
        FROM {SOURCE_SCHEMA}.customers c
        LEFT JOIN _cl_c cl ON c.country = cl.dirty_value
    """)

    return _count_statuses(con, f"{SCHEMA}.clean_customers", logger)

# ---------------------------------------------------------------------------
# clean_bonus_tiers
# ---------------------------------------------------------------------------

def clean_bonus_tiers(
    con: duckdb.DuckDBPyConnection,
    logger: logging.Logger,
) -> dict:
    """
    Validations applied
    ───────────────────
    FAIL  NULL_product_id       product_id IS NULL
    FAIL  INVALID_threshold     threshold IS NOT NULL AND threshold <= 0
    FAIL  INVALID_bonus_amount  bonus_amount IS NOT NULL AND bonus_amount <= 0

    No country column — no country normalisation applied.
    """
    logger.info("Cleaning 'bonus_tiers' ...")

    con.execute(f"""
        CREATE OR REPLACE TABLE {SCHEMA}.clean_bonus_tiers AS
        SELECT
            -- ── Source columns ─────────────────────────────────────────────
            b.product_id, b.product_name, b.threshold, b.bonus_amount,
            b.valid_from, b.valid_to,     b.is_current,
            -- ── Pipeline timestamp ──────────────────────────────────────────
            CURRENT_TIMESTAMP AS cleaned_at,
            -- ── Validation reasons ──────────────────────────────────────────
            NULLIF(CONCAT_WS('|',
                CASE WHEN b.product_id   IS NULL                         THEN 'NULL_product_id'      END,
                CASE WHEN b.threshold    IS NOT NULL AND b.threshold   <= 0 THEN 'INVALID_threshold'   END,
                CASE WHEN b.bonus_amount IS NOT NULL AND b.bonus_amount <= 0 THEN 'INVALID_bonus_amount' END
            ), '') AS validation_reasons,
            -- ── Validation status ───────────────────────────────────────────
            CASE
                WHEN b.product_id IS NULL
                  OR (b.threshold    IS NOT NULL AND b.threshold    <= 0)
                  OR (b.bonus_amount IS NOT NULL AND b.bonus_amount <= 0)
                THEN 'FAIL'
                ELSE 'PASS'
            END AS validation_status
        FROM {SOURCE_SCHEMA}.bonus_tiers b
    """)

    return _count_statuses(con, f"{SCHEMA}.clean_bonus_tiers", logger)

# ---------------------------------------------------------------------------
# CLI + main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage 2: Clean and validate Stage 1 tables in stage_1.duckdb"
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        metavar="0-100",
        help=f"Minimum fuzzy match score for country name acceptance (default: {DEFAULT_THRESHOLD})",
    )
    return parser.parse_args()


def main() -> None:
    args   = parse_args()
    logger = setup_logging()

    logger.info("=" * 60)
    logger.info("Stage 2 started  (full reprocess)")
    logger.info("Database          %s", DB_PATH)
    logger.info("Output schema     %s", SCHEMA)
    logger.info("Fuzzy threshold   %d", args.threshold)
    logger.info("=" * 60)

    if not DB_PATH.exists():
        logger.error("Database not found: %s  — run Stage 1 first.", DB_PATH)
        sys.exit(1)

    con = duckdb.connect(str(DB_PATH))
    initialise_schema(con, logger)

    results = [
        clean_product_sales(con, args.threshold, logger),
        clean_merchants    (con, args.threshold, logger),
        clean_customers    (con, args.threshold, logger),
        clean_bonus_tiers  (con,                logger),
    ]

    con.close()

    # ── Summary ────────────────────────────────────────────────────────────
    logger.info("")
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("%-32s %6s  %6s  %6s  %6s", "TABLE", "TOTAL", "PASS", "WARN", "FAIL")
    logger.info("-" * 60)
    for r in results:
        logger.info(
            "%-32s %6d  %6d  %6d  %6d",
            r["table"], r["total"],
            r.get("PASS", 0), r.get("WARN", 0), r.get("FAIL", 0),
        )
    logger.info("=" * 60)

    total_fail = sum(r.get("FAIL", 0) for r in results)
    if total_fail > 0:
        logger.warning("%d FAIL row(s) found across all tables. Exiting with code 1.", total_fail)
        sys.exit(1)

    logger.info("All rows PASS or WARN. Stage 2 complete.")


if __name__ == "__main__":
    main()
