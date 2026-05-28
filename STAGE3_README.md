# Stage 3 — Integration README

Reads clean rows from `stage_cleanup` (Stage 2) and joins them into one wide
fact table in the `stage_integrate` schema of `stages.duckdb`.

**Pipeline:** [README](README.md) — [Stage 1 (Ingest)](INGEST_README.md) →
[Stage 2 (Cleanup)](STAGE2_README.md) → Stage 3 (Integrate) →
[Stage 4 (Compute)](STAGE4_README.md) → [Stage 5 (Report)](STAGE5_README.md)

Every dimension join is **SCD-aware**: the record that was active on the
`sale_date` is used, not the current snapshot.

---

## Prerequisites

Stages 1 and 2 must have run at least once and `stages.duckdb` must exist.

```bash
python pipeline.py --date YYYY-MM-DD   # Stage 1 — ingest
python stage2.py                       # Stage 2 — clean & validate
```

Install dependencies (once):

```bash
pip install duckdb
```

---

## Usage

```bash
python stage3.py
```

Stage 3 has no arguments — it always processes **all** `PASS` and `WARN` rows
from `stage_cleanup.clean_product_sales`.

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Integration completed (`PARTIAL` joins are logged but do not cause failure) |
| `1` | Fatal error (database not found, SQL failure) |

---

## What it does

1. **Creates** the `stage_integrate` schema if it does not exist.
2. **Drops and rebuilds** `stage_integrate.integrated_sales` on every run
   (full reprocess — idempotent).
3. Reads `stage_cleanup.clean_product_sales` rows with `validation_status IN
   ('PASS', 'WARN')`.  Rows with `validation_status = 'FAIL'` are excluded.
4. Resolves three dimensions against the sale date using SCD-aware lookups:
   - **Merchant / store** — from `stage_cleanup.clean_merchants`
   - **Customer** — from `stage_cleanup.clean_customers`
   - **Bonus tier** — from `stage_cleanup.clean_bonus_tiers`
5. Writes one row per sale into `stage_integrate.integrated_sales`.
6. Logs a `PARTIAL`-join sample (up to 5 rows) when any dimension is unresolved.

---

## SCD-aware join strategy

For each sale, Stage 3 looks for a dimension record whose SCD window covers
the `sale_date`:

```
valid_from <= sale_date  AND  (valid_to > sale_date  OR  valid_to IS NULL)
```

If multiple versions overlap (e.g. two `bonus_tiers` rows both active on the
same date), the version with the **latest `valid_from`** is used. This is
implemented with `ROW_NUMBER() OVER (PARTITION BY sale_id ORDER BY valid_from
DESC NULLS LAST)` and keeping only `_rn = 1`.

Dimension rows with `validation_status = 'FAIL'` are excluded from the lookup
so bad dimension data cannot corrupt the fact table.

All three dimension joins are `LEFT JOIN` — a sale with no matching dimension
record is still included with NULL dimension columns, and its `join_status`
is set to `PARTIAL`.

---

## Output table

### `stage_integrate.integrated_sales`

One row per sale. All columns from `clean_product_sales` plus resolved
dimension attributes and pipeline-added columns.

| Column | Type | Source |
|---|---|---|
| `sale_id` | VARCHAR | sale |
| `partition_date` | DATE | sale |
| `sale_date` | DATE | sale |
| `store_id` | VARCHAR | sale |
| `merchant_id` | VARCHAR | merchant dimension |
| `merchant_name` | VARCHAR | merchant dimension |
| `store_name` | VARCHAR | merchant dimension |
| `merchant_country` | VARCHAR | merchant dimension (`country_clean`) |
| `merchant_valid_from` | DATE | merchant SCD window start |
| `merchant_valid_to` | DATE | merchant SCD window end (NULL = current) |
| `product_id` | VARCHAR | sale |
| `product_name` | VARCHAR | sale |
| `quantity` | INTEGER | sale |
| `unit_price` | DECIMAL(10,2) | sale |
| `total_amount` | DECIMAL(10,2) | sale |
| `customer_id` | VARCHAR | sale |
| `customer_name` | VARCHAR | customer dimension |
| `customer_email` | VARCHAR | customer dimension |
| `customer_registration_date` | DATE | customer dimension |
| `customer_country` | VARCHAR | customer dimension (`country_clean`) |
| `customer_valid_from` | DATE | customer SCD window start |
| `bonus_threshold` | INTEGER | bonus tier dimension |
| `bonus_amount` | DECIMAL(10,2) | bonus tier dimension |
| `bonus_valid_from` | DATE | bonus tier SCD window start |
| `bonus_valid_to` | DATE | bonus tier SCD window end (NULL = current) |
| `sale_country_raw` | VARCHAR | sale (`country` — original dirty value) |
| `sale_country` | VARCHAR | sale (`country_clean` — normalised) |
| `sale_validation_status` | VARCHAR | sale (`PASS` or `WARN`) |
| `sale_validation_reasons` | VARCHAR | sale (pipe-separated check codes, or NULL) |
| `join_status` | VARCHAR | pipeline: `FULL` or `PARTIAL` |
| `unmatched_dimensions` | VARCHAR | pipeline: pipe-separated missing dimension names, or NULL |
| `integrated_at` | TIMESTAMP WITH TIME ZONE | pipeline: timestamp of this Stage 3 run |

### `join_status` values

| Value | Meaning |
|---|---|
| `FULL` | All three dimensions resolved |
| `PARTIAL` | One or more dimensions had no matching record on the sale date |

When `join_status = 'PARTIAL'`, the `unmatched_dimensions` column lists which
dimensions were missing as a pipe-separated string, e.g.
`merchant_not_found|bonus_tier_not_found`.

---

## Querying the output

```sql
-- All fully-joined sales
SELECT *
FROM   stage_integrate.integrated_sales
WHERE  join_status = 'FULL';

-- Sales where a dimension was not found
SELECT sale_id, sale_date, unmatched_dimensions
FROM   stage_integrate.integrated_sales
WHERE  join_status = 'PARTIAL';

-- Total revenue per merchant (FULL joins only)
SELECT merchant_name, SUM(total_amount) AS revenue
FROM   stage_integrate.integrated_sales
WHERE  join_status = 'FULL'
GROUP  BY merchant_name
ORDER  BY revenue DESC;

-- Bonus-eligible sales (total_amount >= bonus_threshold)
SELECT sale_id, product_id, total_amount, bonus_threshold, bonus_amount
FROM   stage_integrate.integrated_sales
WHERE  join_status = 'FULL'
  AND  total_amount >= bonus_threshold;
```

---

## Logging

A new timestamped log file is created on every run:

```
logs/stage3_YYYY-MM-DD_HHMMSS.log
```

| Level | When |
|---|---|
| `INFO` | Row counts — total, FULL, PARTIAL |
| `WARNING` | Sample of up to 5 PARTIAL join rows (sale_id + missing dimensions) |
| `ERROR` | Database not found; SQL failure |

---

## How to re-run

Stage 3 is a full reprocess — simply run it again:

```bash
python stage3.py
```

`integrated_sales` is dropped and rebuilt on every run. No cleanup is needed.

If upstream data has changed, re-run the relevant earlier stage first:

```bash
python pipeline.py --date YYYY-MM-DD   # new partition
python stage2.py                       # re-clean
python stage3.py                       # re-integrate
```

---

## Assumptions

1. **WARN rows are included.** Sales with `validation_status = 'WARN'` (e.g.
   unresolved country) are integrated. Downstream aggregations should filter
   on `sale_country IS NOT NULL` when country accuracy is required.

2. **FAIL rows are excluded.** Sales with `validation_status = 'FAIL'` and
   dimension rows with `validation_status = 'FAIL'` are both suppressed so bad
   data does not corrupt the fact table.

3. **LEFT JOINs preserve all sales.** A sale that has no matching merchant,
   customer, or bonus tier on its sale date is retained with NULL dimension
   columns and `join_status = 'PARTIAL'`. This makes missing dimension
   coverage visible without silently dropping rows.

4. **Latest SCD version wins on overlap.** If two dimension records are both
   active on a given sale date (overlapping SCD windows), the one with the
   later `valid_from` takes precedence. This handles edge cases from back-
   loaded or corrected dimension files.

5. **No date partitioning.** Stage 3 always processes the entire fact table.
   Adding a date filter can be done by passing a `--date` argument and adding
   a `WHERE s.sale_date = ?` predicate to the `sales` CTE.

6. **`bonus_tiers` may have multiple active rows per `product_id`.** If the
   source CSV loads two overlapping records for the same product (e.g. an
   amended bonus schedule), the deduplication rule (latest `valid_from`)
   applies. See Stage 1 documentation for the open question on whether
   `product_id + threshold` should be the composite natural key.
