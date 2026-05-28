"""
pipeline.py  –  Stage 1: Ingest daily CSV partitions into DuckDB (stages.duckdb).

Usage:
    python pipeline.py --date YYYY-MM-DD

Behaviour:
    • Writes all tables into the `stage_ingest` schema of stages.duckdb.
    • Appends rows to the product_sales fact table.
    • Applies SCD Type 2 merge to merchants, customers and bonus_tiers dimension tables.
    • Idempotent: re-running the same date is a no-op (files already marked SUCCESS are skipped).
    • On a missing source file: logs a WARNING and continues with the remaining files.
    • On schema drift: logs an ERROR with a suggested ALTER statement and continues with the
      current table schema (no automatic migration).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NamedTuple

import duckdb

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT_DIR  = Path(__file__).parent
DB_PATH   = ROOT_DIR / "stages.duckdb"
LOG_DIR   = ROOT_DIR / "logs"
DATA_ROOT = ROOT_DIR / "data"
SCHEMA    = "stage_ingest"   # DuckDB schema for all raw ingest tables

FACT      = "FACT"
DIMENSION = "DIMENSION"


class TableConfig(NamedTuple):
    table_name:   str
    subdirectory: str
    file_name:    str
    table_type:   str        # FACT | DIMENSION
    natural_key:  list[str]  # used only for DIMENSION tables


TABLE_REGISTRY: list[TableConfig] = [
    TableConfig("product_sales", "product_sales", "product_sales.csv", FACT,      []),
    TableConfig("merchants",     "merchants",     "merchants.csv",     DIMENSION, ["merchant_id", "store_id"]),
    TableConfig("customers",     "customers",     "customers.csv",     DIMENSION, ["customer_id"]),
    TableConfig("bonus_tiers",   "bonus_tiers",   "bonus_tiers.csv",   DIMENSION, ["product_id"]),
]

# ---------------------------------------------------------------------------
# Explicit column schemas  (source CSV columns + pipeline-added columns)
# If the CSV structure changes, update here and run the suggested ALTER manually.
# ---------------------------------------------------------------------------

SCHEMAS: dict[str, list[tuple[str, str]]] = {
    "product_sales": [
        ("sale_id",        "VARCHAR"),
        ("store_id",       "VARCHAR"),
        ("product_id",     "VARCHAR"),
        ("product_name",   "VARCHAR"),
        ("customer_id",    "VARCHAR"),
        ("quantity",       "INTEGER"),
        ("unit_price",     "DECIMAL(10,2)"),
        ("total_amount",   "DECIMAL(10,2)"),
        ("sale_date",      "DATE"),
        ("country",        "VARCHAR"),
        ("partition_date", "DATE"),
        ("ingested_at",    "TIMESTAMP"),
    ],
    "merchants": [
        ("merchant_id",   "VARCHAR"),
        ("merchant_name", "VARCHAR"),
        ("store_id",      "VARCHAR"),
        ("store_name",    "VARCHAR"),
        ("country",       "VARCHAR"),
        ("valid_from",    "DATE"),
        ("valid_to",      "DATE"),
        ("is_current",    "BOOLEAN"),
    ],
    "customers": [
        ("customer_id",       "VARCHAR"),
        ("customer_name",     "VARCHAR"),
        ("email",             "VARCHAR"),
        ("registration_date", "DATE"),
        ("country",           "VARCHAR"),
        ("valid_from",        "DATE"),
        ("valid_to",          "DATE"),
        ("is_current",        "BOOLEAN"),
    ],
    "bonus_tiers": [
        ("product_id",   "VARCHAR"),
        ("product_name", "VARCHAR"),
        ("threshold",    "INTEGER"),
        ("bonus_amount", "DECIMAL(10,2)"),
        ("valid_from",   "DATE"),
        ("valid_to",     "DATE"),
        ("is_current",   "BOOLEAN"),
    ],
}

# Columns that exist in the source CSV (no pipeline-added columns)
CSV_COLUMNS: dict[str, list[str]] = {
    "product_sales": [
        "sale_id", "store_id", "product_id", "product_name", "customer_id",
        "quantity", "unit_price", "total_amount", "sale_date", "country",
    ],
    "merchants":   ["merchant_id", "merchant_name", "store_id", "store_name", "country"],
    "customers":   ["customer_id", "customer_name", "email", "registration_date", "country"],
    "bonus_tiers": ["product_id", "product_name", "threshold", "bonus_amount"],
}

# Non-key attributes used for change detection in the SCD merge
SCD_ATTRIBUTES: dict[str, list[str]] = {
    "merchants":   ["merchant_name", "store_name", "country"],
    "customers":   ["customer_name", "email", "registration_date", "country"],
    "bonus_tiers": ["product_name", "threshold", "bonus_amount"],
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(partition_date: date) -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"ingest_{partition_date}.log"

    logger = logging.getLogger("ingest")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger

# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def ddl_columns(table_name: str) -> str:
    return ",\n    ".join(f"{col} {dtype}" for col, dtype in SCHEMAS[table_name])


def get_csv_headers(file_path: Path) -> list[str]:
    with open(file_path, encoding="utf-8") as fh:
        raw = fh.readline().strip()
    return [h.strip() for h in raw.split(",")]


def check_schema_drift(table_name: str, file_path: Path, logger: logging.Logger) -> None:
    """
    Compare CSV headers against the known CSV_COLUMNS list.
    Logs an ERROR with a suggested ALTER statement on any mismatch.
    Never raises — pipeline continues regardless.
    """
    expected = set(CSV_COLUMNS[table_name])
    actual   = set(get_csv_headers(file_path))
    added    = actual - expected
    removed  = expected - actual

    if not added and not removed:
        return

    for col in sorted(added):
        logger.error(
            "Schema drift [%s]: column '%s' in CSV but not in schema. "
            "Suggested: ALTER TABLE %s ADD COLUMN %s VARCHAR;",
            table_name, col, table_name, col,
        )
    for col in sorted(removed):
        logger.error(
            "Schema drift [%s]: column '%s' expected but absent from CSV. "
            "Suggested: ALTER TABLE %s DROP COLUMN %s;",
            table_name, col, table_name, col,
        )
    logger.warning(
        "Proceeding with existing schema for '%s'; drifted columns will be ignored.",
        table_name,
    )

# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def initialise_schema(con: duckdb.DuckDBPyConnection, logger: logging.Logger) -> None:
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    logger.debug("Schema '%s' ready.", SCHEMA)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}._ingest_metadata (
            file_path      VARCHAR PRIMARY KEY,
            table_name     VARCHAR  NOT NULL,
            partition_date DATE     NOT NULL,
            rows_loaded    INTEGER,
            ingested_at    TIMESTAMP NOT NULL,
            status         VARCHAR  NOT NULL,
            error_message  VARCHAR
        )
    """)
    for table_name in SCHEMAS:
        col_defs = ddl_columns(table_name)
        con.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{table_name} (\n    {col_defs}\n)")
        logger.debug("Table '%s.%s' ready.", SCHEMA, table_name)

# ---------------------------------------------------------------------------
# Idempotency helpers
# ---------------------------------------------------------------------------

def already_loaded(con: duckdb.DuckDBPyConnection, file_path: Path) -> bool:
    row = con.execute(
        f"SELECT 1 FROM {SCHEMA}._ingest_metadata WHERE file_path = ? AND status = 'SUCCESS'",
        [str(file_path)],
    ).fetchone()
    return row is not None


def record_metadata(
    con: duckdb.DuckDBPyConnection,
    file_path: Path,
    table_name: str,
    partition_date: date,
    rows_loaded: int,
    status: str,
    error_message: str | None = None,
) -> None:
    con.execute(
        f"DELETE FROM {SCHEMA}._ingest_metadata WHERE file_path = ?",
        [str(file_path)],
    )
    con.execute(
        f"""
        INSERT INTO {SCHEMA}._ingest_metadata
            (file_path, table_name, partition_date, rows_loaded, ingested_at, status, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            str(file_path),
            table_name,
            partition_date,
            rows_loaded,
            datetime.now(timezone.utc),
            status,
            error_message,
        ],
    )

# ---------------------------------------------------------------------------
# Fact table loader  (append)
# ---------------------------------------------------------------------------

def load_fact(
    con: duckdb.DuckDBPyConnection,
    cfg: TableConfig,
    file_path: Path,
    partition_date: date,
    logger: logging.Logger,
) -> int:
    """Append all rows from the CSV into the fact table."""
    table    = cfg.table_name
    csv_cols = ", ".join(CSV_COLUMNS[table])
    fp       = str(file_path).replace("'", "''")  # escape single quotes in path

    # Load CSV into a temp staging table (avoids re-reading the file)
    stg = f"_stg_{table}"
    con.execute(f"DROP TABLE IF EXISTS {stg}")
    con.execute(f"CREATE TEMP TABLE {stg} AS SELECT * FROM read_csv_auto('{fp}')")

    rows = con.execute(f"SELECT COUNT(*) FROM {stg}").fetchone()[0]

    con.execute(f"""
        INSERT INTO {SCHEMA}.{table} ({csv_cols}, partition_date, ingested_at)
        SELECT
            {csv_cols},
            DATE '{partition_date}',
            CURRENT_TIMESTAMP
        FROM {stg}
    """)

    logger.info("  [FACT]  '%s': %d rows appended.", table, rows)
    return rows

# ---------------------------------------------------------------------------
# Dimension table loader  (SCD Type 2 full-snapshot merge)
# ---------------------------------------------------------------------------

def load_dimension(
    con: duckdb.DuckDBPyConnection,
    cfg: TableConfig,
    file_path: Path,
    partition_date: date,
    logger: logging.Logger,
) -> int:
    """
    Full-snapshot SCD Type 2 merge.

    Steps:
      1. Load incoming snapshot into a TEMP staging table.
      2. Close records whose natural key is no longer in the snapshot (deleted/transferred).
      3. Close records whose non-key attributes have changed.
      4. Insert new versions for changed records and brand-new natural keys.

    In UPDATE WHERE clauses DuckDB requires the table name (not an alias) when
    referencing the target table in a correlated subquery.
    """
    table     = cfg.table_name
    qtable    = f"{SCHEMA}.{table}"      # fully-qualified name for DDL/DML targets
    nat_key   = cfg.natural_key
    attr_cols = SCD_ATTRIBUTES[table]
    csv_cols  = CSV_COLUMNS[table]
    stg       = f"_stg_{table}"          # TEMP table — no schema prefix needed
    fp        = str(file_path).replace("'", "''")
    pd        = str(partition_date)

    # ---- 1. Staging -------------------------------------------------------
    con.execute(f"DROP TABLE IF EXISTS {stg}")
    con.execute(
        f"CREATE TEMP TABLE {stg} AS SELECT * FROM read_csv_auto('{fp}')"
    )
    stg_rows = con.execute(f"SELECT COUNT(*) FROM {stg}").fetchone()[0]
    logger.debug("  [DIM]   '%s': %d rows in snapshot.", table, stg_rows)

    # Correlated join conditions — use unqualified table name for column refs
    # in correlated subqueries; DuckDB resolves them from the outer query context.
    join_where   = " AND ".join(f"{table}.{k} = s.{k}" for k in nat_key)
    change_where = " OR ".join(
        f"{table}.{col} IS DISTINCT FROM s.{col}" for col in attr_cols
    )

    # ---- 2. Close deleted rows (natural key absent from snapshot) ----------
    n_deleted = con.execute(f"""
        SELECT COUNT(*) FROM {qtable}
        WHERE is_current = TRUE
          AND NOT EXISTS (
              SELECT 1 FROM {stg} s WHERE {join_where}
          )
    """).fetchone()[0]

    if n_deleted:
        con.execute(f"""
            UPDATE {qtable}
               SET valid_to   = DATE '{pd}',
                   is_current = FALSE
             WHERE is_current = TRUE
               AND NOT EXISTS (
                   SELECT 1 FROM {stg} s WHERE {join_where}
               )
        """)

    # ---- 3. Close changed rows --------------------------------------------
    n_changed = con.execute(f"""
        SELECT COUNT(*) FROM {qtable}
        WHERE is_current = TRUE
          AND EXISTS (
              SELECT 1 FROM {stg} s
              WHERE {join_where}
                AND ({change_where})
          )
    """).fetchone()[0]

    if n_changed:
        con.execute(f"""
            UPDATE {qtable}
               SET valid_to   = DATE '{pd}',
                   is_current = FALSE
             WHERE is_current = TRUE
               AND EXISTS (
                   SELECT 1 FROM {stg} s
                   WHERE {join_where}
                     AND ({change_where})
               )
        """)

    # ---- 4. Insert new versions (new keys + re-inserts for changed rows) ---
    col_list = ", ".join(csv_cols)
    s_cols   = ", ".join(f"s.{c}" for c in csv_cols)

    con.execute(f"""
        INSERT INTO {qtable} ({col_list}, valid_from, valid_to, is_current)
        SELECT
            {s_cols},
            DATE '{pd}',
            NULL,
            TRUE
        FROM {stg} s
        WHERE NOT EXISTS (
            SELECT 1 FROM {qtable}
            WHERE {join_where}
              AND {table}.is_current = TRUE
        )
    """)

    n_inserted = con.execute(f"""
        SELECT COUNT(*) FROM {qtable}
        WHERE valid_from = DATE '{pd}' AND is_current = TRUE
    """).fetchone()[0]

    new_records = n_inserted - n_changed   # net-new natural keys
    logger.info(
        "  [DIM]   '%s': %d new, %d changed, %d removed.",
        table, new_records, n_changed, n_deleted,
    )
    return n_inserted

# ---------------------------------------------------------------------------
# Per-file orchestration
# ---------------------------------------------------------------------------

def process_file(
    con: duckdb.DuckDBPyConnection,
    cfg: TableConfig,
    file_path: Path,
    partition_date: date,
    logger: logging.Logger,
) -> dict:
    result = {"table": cfg.table_name, "file": str(file_path),
              "status": None, "rows": 0, "note": ""}

    if already_loaded(con, file_path):
        result.update(status="SKIPPED", note="already loaded")
        logger.info("  [SKIP]  '%s' already loaded.", cfg.table_name)
        return result

    check_schema_drift(cfg.table_name, file_path, logger)

    try:
        con.execute("BEGIN TRANSACTION")

        if cfg.table_type == FACT:
            rows = load_fact(con, cfg, file_path, partition_date, logger)
        else:
            rows = load_dimension(con, cfg, file_path, partition_date, logger)

        record_metadata(con, file_path, cfg.table_name, partition_date, rows, "SUCCESS")
        con.execute("COMMIT")
        result.update(status="SUCCESS", rows=rows)

    except Exception as exc:  # noqa: BLE001
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        error_msg = str(exc)
        logger.error("  [ERROR] '%s': %s", cfg.table_name, error_msg)
        record_metadata(con, file_path, cfg.table_name, partition_date, 0, "FAILED", error_msg)
        result.update(status="FAILED", note=error_msg)

    return result

# ---------------------------------------------------------------------------
# CLI + main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage 1: Ingest daily CSV partitions into stages.duckdb (stage_ingest schema)"
    )
    parser.add_argument(
        "--date", required=True, metavar="YYYY-MM-DD",
        help="Partition date to ingest",
    )
    return parser.parse_args()


def resolve_partition_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        print(f"ERROR: --date must be YYYY-MM-DD, got: {raw!r}", file=sys.stderr)
        sys.exit(1)


def build_file_path(cfg: TableConfig, partition_date: date) -> Path:
    y, m, d = partition_date.strftime("%Y"), partition_date.strftime("%m"), partition_date.strftime("%d")
    return DATA_ROOT / cfg.subdirectory / y / m / d / cfg.file_name


def main() -> None:
    args           = parse_args()
    partition_date = resolve_partition_date(args.date)
    logger         = setup_logging(partition_date)

    logger.info("=" * 60)
    logger.info("Ingest started   partition_date=%s", partition_date)
    logger.info("Database         %s", DB_PATH)
    logger.info("=" * 60)

    con = duckdb.connect(str(DB_PATH))
    initialise_schema(con, logger)

    results = []

    for cfg in TABLE_REGISTRY:
        file_path = build_file_path(cfg, partition_date)
        logger.info("Processing '%s'  →  %s", cfg.table_name, file_path)

        if not file_path.exists():
            logger.warning(
                "  [MISSING] %s not found — skipping '%s', continuing.",
                file_path, cfg.table_name,
            )
            results.append({"table": cfg.table_name, "file": str(file_path),
                             "status": "MISSING", "rows": 0, "note": "file not found"})
            continue

        results.append(process_file(con, cfg, file_path, partition_date, logger))

    con.close()

    logger.info("")
    logger.info("=" * 60)
    logger.info("SUMMARY  partition_date=%s", partition_date)
    logger.info("%-20s %-10s %-8s %s", "TABLE", "STATUS", "ROWS", "NOTE")
    logger.info("-" * 60)
    for r in results:
        logger.info("%-20s %-10s %-8s %s", r["table"], r["status"], r["rows"], r["note"])
    logger.info("=" * 60)

    if any(r["status"] == "FAILED" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
