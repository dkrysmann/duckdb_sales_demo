# Stage 1 — Ingest README

Loads daily CSV partitions into `stages.duckdb` (`stage_ingest` schema) using an append
strategy for fact data and SCD Type 2 for dimension data.

**Pipeline:** [README](README.md) — Stage 1 (Ingest) → [Stage 2 (Cleanup)](STAGE2_README.md) →
[Stage 3 (Integrate)](STAGE3_README.md) → [Stage 4 (Compute)](STAGE4_README.md) →
[Stage 5 (Report)](STAGE5_README.md)

---

## Quick Start

```bash
python pipeline.py --date YYYY-MM-DD
# e.g.
python pipeline.py --date 2026-05-27
```

Dependencies: `duckdb` — install via `pip install -r requirements.txt`.

---

## Source Data Layout

Each of the four source files lives in a date-partitioned directory:

```
data/
├── product_sales/YYYY/MM/DD/product_sales.csv   ← fact (append)
├── merchants/YYYY/MM/DD/merchants.csv            ← dimension (SCD Type 2)
├── customers/YYYY/MM/DD/customers.csv            ← dimension (SCD Type 2)
└── bonus_tiers/YYYY/MM/DD/bonus_tiers.csv        ← dimension (SCD Type 2)
```

Each daily file represents:
- **product_sales** — new transactions for that day only (delta).
- **merchants / customers / bonus_tiers** — a **full snapshot** of all currently active
  records as of that day. The pipeline compares this snapshot against the database to
  detect additions, changes and removals.

### Column Schemas

#### `product_sales`
| Column | Type | Notes |
|---|---|---|
| `sale_id` | VARCHAR | Unique transaction ID |
| `store_id` | VARCHAR | FK → merchants |
| `product_id` | VARCHAR | FK → bonus_tiers |
| `product_name` | VARCHAR | |
| `customer_id` | VARCHAR | FK → customers |
| `quantity` | INTEGER | |
| `unit_price` | DECIMAL(10,2) | |
| `total_amount` | DECIMAL(10,2) | |
| `sale_date` | DATE | |
| `country` | VARCHAR | **Intentionally dirty** — misspellings & special chars; normalised in Stage 2 |

#### `merchants`
| Column | Type | Notes |
|---|---|---|
| `merchant_id` | VARCHAR | One merchant may appear on multiple rows (one per store) |
| `merchant_name` | VARCHAR | |
| `store_id` | VARCHAR | Unique store identifier |
| `store_name` | VARCHAR | |
| `country` | VARCHAR | |

#### `customers`
| Column | Type | Notes |
|---|---|---|
| `customer_id` | VARCHAR | |
| `customer_name` | VARCHAR | |
| `email` | VARCHAR | |
| `registration_date` | DATE | |
| `country` | VARCHAR | |

#### `bonus_tiers`
| Column | Type | Notes |
|---|---|---|
| `product_id` | VARCHAR | |
| `product_name` | VARCHAR | |
| `threshold` | INTEGER | Minimum units a merchant must sell to earn the bonus |
| `bonus_amount` | DECIMAL(10,2) | Fixed bonus paid when threshold is met |

---

## Database: `stages.duckdb` / schema `stage_ingest`

Located in the project root. Five tables are created automatically on first run.

### Fact table: `product_sales`
Rows are **appended** every run. Two pipeline columns are added:

| Added column | Type | Meaning |
|---|---|---|
| `partition_date` | DATE | Date partition the row came from |
| `ingested_at` | TIMESTAMP | UTC timestamp of the ingest run |

### Dimension tables: `merchants`, `customers`, `bonus_tiers`
Managed with **SCD Type 2**. Three pipeline columns are added:

| Added column | Type | Meaning |
|---|---|---|
| `valid_from` | DATE | First day this version was the truth |
| `valid_to` | DATE | First day it was superseded (`NULL` = still active) |
| `is_current` | BOOLEAN | `TRUE` for the currently active version |

**Natural keys** used for change detection:

| Table | Natural key |
|---|---|
| `merchants` | `merchant_id` + `store_id` |
| `customers` | `customer_id` |
| `bonus_tiers` | `product_id` |

To query only the current state of a dimension table, filter on `is_current = TRUE`.

### Tracking table: `_ingest_metadata`
Records every load attempt. Drives idempotency.

| Column | Meaning |
|---|---|
| `file_path` | Full path to the CSV (primary key) |
| `table_name` | Target table |
| `partition_date` | Partition being loaded |
| `rows_loaded` | Row count after the operation |
| `ingested_at` | UTC timestamp |
| `status` | `SUCCESS`, `FAILED`, or (implied by absence) not yet run |
| `error_message` | Populated on `FAILED` |

---

## Idempotency

Running the pipeline for the same `--date` twice is safe and produces no changes.

Before loading each file the pipeline checks `_ingest_metadata` for a `SUCCESS` record
matching that file path. If one exists the file is **skipped**. A `FAILED` record causes
a retry on the next run.

---

## SCD Type 2 Merge Logic

On each run for a dimension table the pipeline performs four steps:

1. **Stage** — loads the incoming CSV snapshot into a temporary in-memory table.
2. **Close deleted rows** — sets `valid_to = partition_date`, `is_current = FALSE` for
   any currently active row whose natural key is absent from today's snapshot.
   *Example: a store transferred away from a merchant disappears from the snapshot; the
   old `(merchant_id, store_id)` row is closed.*
3. **Close changed rows** — same closure for rows whose natural key is present but any
   non-key attribute differs.
   *Example: bonus threshold for a product changes; the old row is closed.*
4. **Insert new versions** — inserts all snapshot rows whose natural key has no currently
   active record in the database. This covers both brand-new keys and the new versions of
   changed rows.

Unchanged rows (natural key matches and all attributes are identical) generate no writes.

---

## Missing Files

If a source file is not found for the requested date the pipeline logs a `WARNING` and
continues loading the remaining files. It does **not** abort the entire run.

---

## Schema Drift

The column schemas for all four tables are defined explicitly in `pipeline.py` under
`SCHEMAS` and `CSV_COLUMNS`. The pipeline compares CSV headers against the expected list
before each load.

If a mismatch is detected:
- An `ERROR` is logged for each unexpected or missing column.
- A suggested `ALTER TABLE` statement is included in the log message.
- **The pipeline continues with the current schema** — no automatic migration is applied.

To apply a schema change:

1. Run the suggested `ALTER TABLE` statement against `stages.duckdb`.
2. Update `SCHEMAS` and `CSV_COLUMNS` in `pipeline.py` to match.
3. Re-run the pipeline normally.

---

## Logging

One log file is written per run date:

```
logs/ingest_YYYY-MM-DD.log
```

Multiple runs on the same date **append** to the same file. Each line is timestamped.
The log is also echoed to stdout.

Log levels used:

| Level | When |
|---|---|
| `DEBUG` | Row counts per staging table; table-ready confirmations |
| `INFO` | Per-table load results; run summary |
| `WARNING` | Missing files; schema drift (non-blocking) |
| `ERROR` | Schema drift details; load failures |

---

## Sample Data

Three days of sample data are provided for testing (2026-05-25 through 2026-05-27).
The data demonstrates the following scenarios:

| Day | Event | Table affected |
|---|---|---|
| 2026-05-25 | Initial load — 6 merchants, 8 customers, 5 bonus tiers, 14 sales | All |
| 2026-05-26 | Emma Wilson (M005) gains a second store (S007 ByteManchester) | `merchants` |
| 2026-05-27 | Mouse (P002) bonus threshold lowered from 50 to 40 units | `bonus_tiers` |

Dirty country values in `product_sales` (e.g. `Germny`, `Germ@ny`, `Franc3`, `Jap@n`,
`U.K.`, `Uni+ed Kingdom`) are left uncorrected at this stage — country normalisation is
handled in Stage 2.

Customers C006 and C008 purchase only on day 1, making them churn candidates for the
Stage 4 churn analysis.

---

## Assumptions

1. **Full snapshot for dimensions** — each daily merchants/customers/bonus_tiers file
   contains *all* active records for that day, not just changes.
2. **`valid_to` is exclusive** — `valid_to = 2026-05-27` means the record was valid
   through 2026-05-26. To find records active on a given date `D`:
   `WHERE valid_from <= D AND (valid_to > D OR valid_to IS NULL)`.
3. **Bonus is per merchant, not per store** — a merchant's total units sold across all
   their stores are summed against the threshold (relevant to Stage 4).
4. **Currency** — all monetary values are assumed to be in a single currency.
   Multi-currency support is out of scope for Stage 1.
5. **No deduplication on facts** — `product_sales` is append-only; duplicate `sale_id`
   values from different partitions are not removed at ingest. Deduplication, if needed,
   is a Stage 2 concern.
6. **No backfill ordering guarantee** — if partitions are loaded out of order (e.g. day 3
   before day 2) the SCD `valid_from` / `valid_to` dates will reflect the load order, not
   necessarily the calendar order. Load partitions in chronological order for correct history.
