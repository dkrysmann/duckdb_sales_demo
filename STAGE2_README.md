# Stage 2 — Data Cleaning & Validation README

Reads raw tables from `stages.duckdb` (`stage_ingest` schema, written by Stage 1).
Produces four `clean_*` tables in the `stage_cleanup` schema of the same database.
Full reprocess: every run drops and rebuilds all clean tables from scratch.

**Pipeline:** [README](README.md) — [Stage 1 (Ingest)](INGEST_README.md) → Stage 2 (Cleanup) →
[Stage 3 (Integrate)](STAGE3_README.md) → [Stage 4 (Compute)](STAGE4_README.md) →
[Stage 5 (Report)](STAGE5_README.md)

---

## Prerequisites

Stage 1 must have run at least once and `stages.duckdb` must exist in the project root.

```bash
python pipeline.py --date YYYY-MM-DD   # run Stage 1 first
```

Install dependencies (once):

```bash
pip install duckdb pycountry rapidfuzz openpyxl
```

---

## Usage

```bash
python stage2.py                   # default fuzzy threshold = 80
python stage2.py --threshold 75    # lower threshold — accepts weaker matches
python stage2.py --threshold 90    # stricter — more WARN rows for borderline spellings
```

| Argument | Default | Meaning |
|---|---|---|
| `--threshold` | `80` | Minimum fuzzy match score (0–100) for a country name to be accepted. Rows below this threshold receive `validation_status = WARN`. |

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | All rows are `PASS` or `WARN` |
| `1` | One or more rows have `validation_status = FAIL` |

---

## Output Tables (all in `stages.duckdb`, schema `stage_cleanup`)

| Source table | Clean table | Notes |
|---|---|---|
| `product_sales` | `clean_product_sales` | Country normalised + FK checks |
| `merchants` | `clean_merchants` | Country normalised |
| `customers` | `clean_customers` | Country normalised + email format check |
| `bonus_tiers` | `clean_bonus_tiers` | Numeric range checks only |

Each clean table contains **all columns from its source table** plus the following pipeline-added columns.

### Columns added to all clean tables

| Column | Type | Meaning |
|---|---|---|
| `cleaned_at` | TIMESTAMP | UTC timestamp of the Stage 2 run that created this row |
| `validation_status` | VARCHAR | `PASS`, `WARN`, or `FAIL` (see below) |
| `validation_reasons` | VARCHAR | Pipe-separated list of failed check codes, e.g. `NULL_store_id\|FK_store_not_found`. `NULL` when all checks pass. |

### Columns added to `clean_product_sales`, `clean_merchants`, `clean_customers`

| Column | Type | Meaning |
|---|---|---|
| `country_clean` | VARCHAR | Correctly spelled ISO country name, or `NULL` if the fuzzy match score was below the threshold |
| `country_match_score` | INTEGER | Fuzzy match confidence 0–100. `100` = exact match or alias hit. |

---

## Validation Status

| Status | When | Effect on downstream |
|---|---|---|
| `PASS` | All checks pass | Row is ready for Stage 3 |
| `WARN` | Non-blocking issue detected (e.g. country could not be matched confidently, email format suspect) | Row is passed through; downstream stages should treat `country_clean = NULL` rows with care |
| `FAIL` | Blocking issue (NULL in required column, invalid price, FK violation) | Row is still present in the clean table but should not be used in aggregations |

All failed check codes for a row are recorded in `validation_reasons` — a row is **not** stopped after its first failure.

To query rows needing attention:

```sql
-- All rows that failed a critical check
SELECT * FROM clean_product_sales WHERE validation_status = 'FAIL';

-- Country values that could not be resolved
SELECT DISTINCT country, country_match_score
FROM clean_product_sales
WHERE country_clean IS NULL
ORDER BY country_match_score;
```

---

## Validation Checks

### `clean_product_sales`

| Check code | Category | Condition | Status |
|---|---|---|---|
| `NULL_sale_id` | Structural | `sale_id IS NULL` | FAIL |
| `NULL_store_id` | Structural | `store_id IS NULL` | FAIL |
| `NULL_product_id` | Structural | `product_id IS NULL` | FAIL |
| `NULL_customer_id` | Structural | `customer_id IS NULL` | FAIL |
| `NULL_sale_date` | Structural | `sale_date IS NULL` | FAIL |
| `INVALID_unit_price` | Format / business rule | `unit_price <= 0` | FAIL |
| `FK_store_not_found` | Referential integrity | `store_id` has no active record in `merchants` that covers `sale_date` (SCD-aware) | FAIL |
| `FK_customer_not_found` | Referential integrity | `customer_id` has no active record in `customers` at or before `sale_date` (SCD-aware) | FAIL |
| `LOW_COUNTRY_MATCH_SCORE` | Format / encoding | Fuzzy match score < threshold | WARN |

**SCD-aware FK check:** a merchant or customer record is considered active for a sale if
`valid_from ≤ sale_date AND (valid_to > sale_date OR valid_to IS NULL)`.
A sale linked to a store that was not yet open, or a customer who did not yet exist, will
receive `FK_store_not_found` or `FK_customer_not_found`.

### `clean_merchants`

| Check code | Category | Condition | Status |
|---|---|---|---|
| `NULL_merchant_id` | Structural | `merchant_id IS NULL` | FAIL |
| `NULL_store_id` | Structural | `store_id IS NULL` | FAIL |
| `LOW_COUNTRY_MATCH_SCORE` | Format / encoding | Fuzzy match score < threshold | WARN |

### `clean_customers`

| Check code | Category | Condition | Status |
|---|---|---|---|
| `NULL_customer_id` | Structural | `customer_id IS NULL` | FAIL |
| `NULL_registration_date` | Structural | `registration_date IS NULL` | FAIL |
| `INVALID_EMAIL_FORMAT` | Format / encoding | `email` does not match pattern `*@*.*` | WARN |
| `LOW_COUNTRY_MATCH_SCORE` | Format / encoding | Fuzzy match score < threshold | WARN |

### `clean_bonus_tiers`

| Check code | Category | Condition | Status |
|---|---|---|---|
| `NULL_product_id` | Structural | `product_id IS NULL` | FAIL |
| `INVALID_threshold` | Format / business rule | `threshold <= 0` | FAIL |
| `INVALID_bonus_amount` | Format / business rule | `bonus_amount <= 0` | FAIL |

---

## Country Normalisation

Applied to `product_sales.country`, `merchants.country`, and `customers.country`.

### Four-step process per unique dirty value

```
Raw value (e.g. "Germ@ny", "GERMANY", "U.K.")
   │
   ▼
Step 1 — Strip non-alphanumeric characters (replace with space)
           "Germ@ny" → "Germ ny"    "U.K." → "U K"
   │
   ▼
Step 2 — Collapse multiple spaces and trim
           "Germ ny" → "Germ ny"    "U K" → "U K"
   │
   ▼
Step 3 — Alias lookup (before fuzzy matching)
           Collapse spaces and lowercase: "u k" → "uk"
           Check against _COUNTRY_ALIASES dict:
             "uk"  → "United Kingdom"   (score 100)
             "usa" → "United States"    (score 100)
             "uae" → "United Arab Emirates"
           If matched → use canonical name, skip Step 4
   │
   ▼
Step 4 — Fuzzy match against pycountry ISO 3166-1 names
           Uses rapidfuzz WRatio scorer with case-insensitive processing
           "Germ ny" → "Germany"  (score 92)
           "JAPAN"   → "Japan"    (score 100)
           "Franc3"  → "France"   (score 83)

           score ≥ threshold  →  country_clean = canonical name
           score <  threshold →  country_clean = NULL, validation_status = WARN
```

### Performance

All unique dirty values across the whole table are resolved **once** into a lookup
dictionary before rows are written. The fuzzy matcher is not called once per row.

### Extending the alias list

Add entries to `_COUNTRY_ALIASES` in `stage2.py` for any common abbreviations or
custom spellings that fuzzy matching does not handle well:

```python
_COUNTRY_ALIASES: dict[str, str] = {
    "uk":  "United Kingdom",
    "gb":  "United Kingdom",
    "usa": "United States",
    "us":  "United States",
    ...
}
```

Keys must be **lowercase with spaces removed** (the script normalises incoming values
the same way before lookup).

---

## Logging

A new timestamped log file is created on every run:

```
logs/stage2_YYYY-MM-DD_HHMMSS.log
```

Multiple runs never overwrite each other.

| Level | When |
|---|---|
| `DEBUG` | Each unique country value and its match result |
| `INFO` | Per-table row counts and PASS/WARN/FAIL summary |
| `WARNING` | Country values that could not be matched; values empty after normalisation |
| `ERROR` | Database not found |

To see only the summary without debug output, redirect or filter:

```bash
python stage2.py 2>&1 | grep -E "INFO|WARNING|ERROR"
```

---

## How to Re-run After Source Data Changes

Stage 2 is a **full reprocess** — simply run it again:

```bash
python stage2.py
```

All `clean_*` tables are recreated from the current state of the Stage 1 source tables.
No cleanup is needed.

If Stage 1 source data has changed (new partitions loaded), run Stage 1 first:

```bash
python pipeline.py --date 2026-05-28   # load new partition
python stage2.py                       # re-clean everything
```

---

## Assumptions

1. **WARN rows are not removed.** Every row from Stage 1 appears in the corresponding
   clean table. `FAIL` rows should be excluded from downstream aggregations by filtering
   on `validation_status != 'FAIL'`.

2. **FK checks are SCD-aware.** A `store_id` in `product_sales` is validated against the
   `merchants` SCD history using the `sale_date` as the point-in-time reference.
   A sale dated before a store was opened will be flagged.

3. **Email check is lenient.** The pattern `*@*.*` catches obvious formatting errors only
   (missing `@`, missing domain). Full RFC 5322 validation is out of scope.

4. **Country normalisation is per table, not global.** The same dirty value in
   `product_sales` and `merchants` is resolved independently. In practice the results
   will be identical since the same pycountry list is used.

5. **Fuzzy threshold is global.** One threshold value applies to all three tables.
   If finer control is needed per table, update `clean_product_sales`,
   `clean_merchants`, and `clean_customers` calls to pass different values.

6. **`bonus_tiers` duplicate `product_id` rows** visible in the clean table are carried
   forward from Stage 1 as-is. If a product has two active SCD records with the same
   `product_id` (e.g. from modified source files), both appear in `clean_bonus_tiers`
   with `is_current = TRUE`. Stage 4 bonus calculation must account for this — see the
   open question in the Stage 1 plan about whether `product_id + threshold` should be
   the natural key.
