"""
stage4.py — Stage 4: Compute

Reads the integrated fact table from stage_integrate (Stage 3) and builds
four analytical tables in the stage_compute schema.

    stages.duckdb
    ├── stage_ingest     ← Stage 1 raw tables
    ├── stage_cleanup    ← Stage 2 clean tables
    ├── stage_integrate  ← Stage 3 wide fact table
    └── stage_compute    ← Stage 4 analytical tables
        ├── sales_kpis         revenue / volume KPIs per store per day
        ├── bonus_results      per-sale bonus eligibility + merchant aggregates
        ├── customer_metrics   lifetime metrics per customer
        └── churn_flags        inactivity-based churn classification

Full reprocess: drops and rebuilds all stage_compute tables on every run.
No date argument — always processes all FULL-join rows from stage_integrate.

Usage:
    python stage4.py
    python stage4.py --inactivity-days 7

Arguments:
    --inactivity-days N   Days without a purchase before a customer is
                          considered churned. Default: 1.

Exit codes:
    0  Compute completed
    1  Fatal error (database not found, SQL failure)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import duckdb

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT_DIR       = Path(__file__).parent
DB_PATH        = ROOT_DIR / "stages.duckdb"
LOG_DIR        = ROOT_DIR / "logs"
SCHEMA         = "stage_compute"
SOURCE_SCHEMA  = "stage_integrate"

DEFAULT_INACTIVITY_DAYS = 1   # minimum days of silence before churn flag

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    run_ts   = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = LOG_DIR / f"stage4_{run_ts}.log"

    logger = logging.getLogger("stage4")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            "%Y-%m-%d %H:%M:%S")
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

def initialise_schema(con: duckdb.DuckDBPyConnection,
                      logger: logging.Logger) -> None:
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    logger.info("Schema '%s' ready.", SCHEMA)

# ---------------------------------------------------------------------------
# Table builders
# ---------------------------------------------------------------------------

def build_sales_kpis(con: duckdb.DuckDBPyConnection,
                     logger: logging.Logger) -> int:
    """
    sales_kpis — revenue and volume metrics per store per day.

    Granularity: (merchant_id, store_id, sale_date)
    Source:      stage_integrate.integrated_sales WHERE join_status = 'FULL'
    """
    logger.info("Building '%s.sales_kpis' ...", SCHEMA)

    con.execute(f"""
        CREATE OR REPLACE TABLE {SCHEMA}.sales_kpis AS
        SELECT
            merchant_id,
            merchant_name,
            store_id,
            store_name,
            merchant_country,
            sale_date,

            -- Volume
            COUNT(*)                            AS transaction_count,
            SUM(quantity)                       AS units_sold,
            COUNT(DISTINCT customer_id)         AS unique_customers,
            COUNT(DISTINCT product_id)          AS distinct_products,

            -- Revenue
            SUM(total_amount)                   AS total_revenue,
            AVG(total_amount)                   AS avg_order_value,
            MIN(total_amount)                   AS min_order_value,
            MAX(total_amount)                   AS max_order_value,

            -- Validation provenance
            SUM(CASE WHEN sale_validation_status = 'WARN' THEN 1 ELSE 0 END)
                                                AS warn_row_count,

            CURRENT_TIMESTAMP                   AS computed_at

        FROM   {SOURCE_SCHEMA}.integrated_sales
        WHERE  join_status = 'FULL'
        GROUP  BY
            merchant_id, merchant_name,
            store_id, store_name,
            merchant_country, sale_date
        ORDER  BY sale_date, merchant_id, store_id
    """)

    n = con.execute(
        f"SELECT COUNT(*) FROM {SCHEMA}.sales_kpis"
    ).fetchone()[0]
    logger.info("  sales_kpis: %d rows.", n)
    return n


def build_bonus_results(con: duckdb.DuckDBPyConnection,
                        logger: logging.Logger) -> dict:
    """
    bonus_results — two outputs from one source scan:
        • per_sale_bonus   : one row per sale, with bonus eligibility flag
        • merchant_bonus   : aggregate bonus earned per merchant per day

    A sale is bonus-eligible when:
        total_amount >= bonus_threshold  AND  bonus_threshold IS NOT NULL
    """
    logger.info("Building '%s.bonus_results' ...", SCHEMA)

    # ── Per-sale bonus eligibility ─────────────────────────────────────────
    con.execute(f"""
        CREATE OR REPLACE TABLE {SCHEMA}.per_sale_bonus AS
        SELECT
            sale_id,
            sale_date,
            merchant_id,
            merchant_name,
            store_id,
            store_name,
            customer_id,
            customer_name,
            product_id,
            product_name,
            quantity,
            unit_price,
            total_amount,
            bonus_threshold,
            bonus_amount,

            CASE
                WHEN bonus_threshold IS NOT NULL
                 AND total_amount >= bonus_threshold  THEN TRUE
                ELSE FALSE
            END                                     AS is_bonus_eligible,

            CASE
                WHEN bonus_threshold IS NOT NULL
                 AND total_amount >= bonus_threshold  THEN bonus_amount
                ELSE 0
            END                                     AS bonus_earned,

            CURRENT_TIMESTAMP                       AS computed_at

        FROM   {SOURCE_SCHEMA}.integrated_sales
        WHERE  join_status = 'FULL'
        ORDER  BY sale_date, merchant_id, sale_id
    """)

    # ── Merchant-level bonus aggregates ───────────────────────────────────
    con.execute(f"""
        CREATE OR REPLACE TABLE {SCHEMA}.merchant_bonus AS
        SELECT
            merchant_id,
            merchant_name,
            store_id,
            store_name,
            sale_date,

            COUNT(*)                                        AS total_sales,
            SUM(CASE WHEN is_bonus_eligible THEN 1 ELSE 0 END)
                                                            AS eligible_sales,
            SUM(bonus_earned)                               AS total_bonus_earned,
            SUM(total_amount)                               AS total_revenue,
            ROUND(
                100.0 * SUM(CASE WHEN is_bonus_eligible THEN 1 ELSE 0 END)
                      / NULLIF(COUNT(*), 0),
                2
            )                                               AS bonus_hit_rate_pct,

            CURRENT_TIMESTAMP                               AS computed_at

        FROM   {SCHEMA}.per_sale_bonus
        GROUP  BY merchant_id, merchant_name, store_id, store_name, sale_date
        ORDER  BY sale_date, merchant_id, store_id
    """)

    n_sale   = con.execute(f"SELECT COUNT(*) FROM {SCHEMA}.per_sale_bonus").fetchone()[0]
    n_merch  = con.execute(f"SELECT COUNT(*) FROM {SCHEMA}.merchant_bonus").fetchone()[0]
    eligible = con.execute(
        f"SELECT SUM(CASE WHEN is_bonus_eligible THEN 1 ELSE 0 END) FROM {SCHEMA}.per_sale_bonus"
    ).fetchone()[0] or 0

    logger.info(
        "  per_sale_bonus: %d rows  |  eligible=%d  (%.1f%%)",
        n_sale, eligible, 100.0 * eligible / n_sale if n_sale else 0,
    )
    logger.info("  merchant_bonus: %d rows.", n_merch)
    return {"per_sale_bonus": n_sale, "merchant_bonus": n_merch, "eligible": eligible}


def build_customer_metrics(con: duckdb.DuckDBPyConnection,
                           logger: logging.Logger) -> int:
    """
    customer_metrics — lifetime purchase behaviour per customer.

    Granularity: one row per customer_id.
    """
    logger.info("Building '%s.customer_metrics' ...", SCHEMA)

    con.execute(f"""
        CREATE OR REPLACE TABLE {SCHEMA}.customer_metrics AS
        SELECT
            customer_id,
            MAX(customer_name)          AS customer_name,
            MAX(customer_email)         AS customer_email,
            MAX(customer_country)       AS customer_country,
            MAX(customer_registration_date) AS registration_date,

            MIN(sale_date)              AS first_purchase_date,
            MAX(sale_date)              AS last_purchase_date,

            COUNT(*)                    AS total_transactions,
            SUM(quantity)               AS total_units_bought,
            SUM(total_amount)           AS total_spend,
            AVG(total_amount)           AS avg_order_value,

            COUNT(DISTINCT store_id)    AS stores_visited,
            COUNT(DISTINCT product_id)  AS distinct_products_bought,

            -- Days between first and last purchase (loyalty span)
            (MAX(sale_date) - MIN(sale_date))
                                        AS purchase_span_days,

            CURRENT_TIMESTAMP           AS computed_at

        FROM   {SOURCE_SCHEMA}.integrated_sales
        WHERE  join_status = 'FULL'
        GROUP  BY customer_id
        ORDER  BY total_spend DESC
    """)

    n = con.execute(
        f"SELECT COUNT(*) FROM {SCHEMA}.customer_metrics"
    ).fetchone()[0]
    logger.info("  customer_metrics: %d customers.", n)
    return n


def build_churn_flags(con: duckdb.DuckDBPyConnection,
                      inactivity_days: int,
                      logger: logging.Logger) -> dict:
    """
    churn_flags — one row per customer with inactivity-based churn flag.

    A customer is flagged as churned when:
        (reference_date - last_purchase_date) > inactivity_days

    reference_date = MAX(sale_date) across all integrated_sales
    (i.e. the most recent day in the loaded data).

    inactivity_days is configurable via --inactivity-days (default: 1).
    """
    logger.info(
        "Building '%s.churn_flags' (inactivity_days=%d) ...",
        SCHEMA, inactivity_days,
    )

    con.execute(f"""
        CREATE OR REPLACE TABLE {SCHEMA}.churn_flags AS
        WITH ref AS (
            SELECT MAX(sale_date) AS reference_date
            FROM   {SOURCE_SCHEMA}.integrated_sales
            WHERE  join_status = 'FULL'
        )
        SELECT
            cm.customer_id,
            cm.customer_name,
            cm.customer_email,
            cm.customer_country,
            cm.first_purchase_date,
            cm.last_purchase_date,
            cm.total_transactions,
            cm.total_spend,

            ref.reference_date,
            (ref.reference_date - cm.last_purchase_date)
                                            AS days_since_last_purchase,
            {inactivity_days}               AS inactivity_threshold_days,

            CASE
                WHEN (ref.reference_date - cm.last_purchase_date) > {inactivity_days}
                THEN TRUE
                ELSE FALSE
            END                             AS is_churned,

            CURRENT_TIMESTAMP               AS computed_at

        FROM   {SCHEMA}.customer_metrics cm
        CROSS  JOIN ref
        ORDER  BY days_since_last_purchase DESC, cm.last_purchase_date ASC
    """)

    total   = con.execute(f"SELECT COUNT(*) FROM {SCHEMA}.churn_flags").fetchone()[0]
    churned = con.execute(
        f"SELECT COUNT(*) FROM {SCHEMA}.churn_flags WHERE is_churned"
    ).fetchone()[0]
    ref_date = con.execute(
        f"SELECT MAX(reference_date) FROM {SCHEMA}.churn_flags"
    ).fetchone()[0]

    logger.info(
        "  churn_flags: %d customers  |  churned=%d  active=%d  "
        "(reference_date=%s  threshold=%d days)",
        total, churned, total - churned, ref_date, inactivity_days,
    )
    return {"total": total, "churned": churned, "active": total - churned}

# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage 4: Compute analytical tables from integrated_sales"
    )
    parser.add_argument(
        "--inactivity-days",
        type=int,
        default=DEFAULT_INACTIVITY_DAYS,
        metavar="N",
        help=(
            f"Days without a purchase before a customer is flagged as churned "
            f"(default: {DEFAULT_INACTIVITY_DAYS})"
        ),
    )
    return parser.parse_args()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args   = parse_args()
    logger = setup_logging()

    logger.info("=" * 60)
    logger.info("Stage 4 started  (full reprocess)")
    logger.info("Database          %s", DB_PATH)
    logger.info("Source schema     %s", SOURCE_SCHEMA)
    logger.info("Output schema     %s", SCHEMA)
    logger.info("Inactivity days   %d", args.inactivity_days)
    logger.info("=" * 60)

    if not DB_PATH.exists():
        logger.error(
            "Database not found: %s  — run Stages 1–3 first.", DB_PATH
        )
        sys.exit(1)

    con = duckdb.connect(str(DB_PATH))

    try:
        initialise_schema(con, logger)
        n_kpis    = build_sales_kpis(con, logger)
        b_stats   = build_bonus_results(con, logger)
        n_cust    = build_customer_metrics(con, logger)
        c_stats   = build_churn_flags(con, args.inactivity_days, logger)
    except Exception as exc:  # noqa: BLE001
        logger.error("Stage 4 failed: %s", exc)
        con.close()
        sys.exit(1)

    con.close()

    logger.info("")
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("  sales_kpis         : %d store-day rows", n_kpis)
    logger.info("  per_sale_bonus     : %d rows  (%d eligible)",
                b_stats["per_sale_bonus"], b_stats["eligible"])
    logger.info("  merchant_bonus     : %d merchant-day rows",
                b_stats["merchant_bonus"])
    logger.info("  customer_metrics   : %d customers", n_cust)
    logger.info("  churn_flags        : %d customers  (%d churned)",
                c_stats["total"], c_stats["churned"])
    logger.info("=" * 60)
    logger.info("Stage 4 complete.")


if __name__ == "__main__":
    main()
