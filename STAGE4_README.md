# Stage 4 — Compute README

**Pipeline:** [README](README.md) — [Stage 1 (Ingest)](INGEST_README.md) →
[Stage 2 (Cleanup)](STAGE2_README.md) → [Stage 3 (Integrate)](STAGE3_README.md) →
Stage 4 (Compute) → [Stage 5 (Report)](STAGE5_README.md)

Reads the integrated fact table from `stage_integrate` (Stage 3) and
produces four analytical tables in the `stage_compute` schema of `stages.duckdb`.

    stages.duckdb
    ├── stage_ingest     ← Stage 1 raw tables
    ├── stage_cleanup    ← Stage 2 clean tables
    ├── stage_integrate  ← Stage 3 wide fact table
    └── stage_compute    ← Stage 4 output
        ├── sales_kpis         revenue / volume KPIs per store per day
        ├── per_sale_bonus     per-sale bonus eligibility
        ├── merchant_bonus     bonus aggregates per merchant per day
        ├── customer_metrics   lifetime behaviour per customer
        └── churn_flags        inactivity-based churn classification

Full reprocess: all `stage_compute` tables are dropped and rebuilt on every run.

---

## Prerequisites

Stages 1–3 must have run successfully and `stages.duckdb` must exist.

```bash
python pipeline.py --date YYYY-MM-DD   # Stage 1
python stage2.py                       # Stage 2
python stage3.py                       # Stage 3
```

---

## Usage

```bash
python stage4.py                       # default inactivity window = 1 day
python stage4.py --inactivity-days 7   # flag customers silent for > 7 days
```

| Argument | Default | Meaning |
|---|---|---|
| `--inactivity-days` | `1` | Days without a purchase before a customer is flagged as churned. The reference date is `MAX(sale_date)` in the integrated table. |

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | All compute tables built successfully |
| `1` | Fatal error (database not found, SQL failure) |

---

## Source data

All five output tables are built from `stage_integrate.integrated_sales`.

**Row filter:** `join_status = 'FULL'` — rows where any dimension was
unresolved (`PARTIAL`) are excluded from all computations to prevent
null-contaminated aggregates.

---

## Output tables

### `stage_compute.sales_kpis`

Revenue and volume metrics per store per day.  
**Granularity:** `(merchant_id, store_id, sale_date)`

| Column | Type | Description |
|---|---|---|
| `merchant_id` | VARCHAR | Merchant identifier |
| `merchant_name` | VARCHAR | Merchant name |
| `store_id` | VARCHAR | Store identifier |
| `store_name` | VARCHAR | Store name |
| `merchant_country` | VARCHAR | Normalised country name |
| `sale_date` | DATE | Sale date |
| `transaction_count` | BIGINT | Number of sales on this day |
| `units_sold` | BIGINT | Total quantity sold |
| `unique_customers` | BIGINT | Distinct customer count |
| `distinct_products` | BIGINT | Distinct product count |
| `total_revenue` | DOUBLE | Sum of `total_amount` |
| `avg_order_value` | DOUBLE | Average `total_amount` |
| `min_order_value` | DOUBLE | Minimum `total_amount` |
| `max_order_value` | DOUBLE | Maximum `total_amount` |
| `warn_row_count` | BIGINT | Sales with `sale_validation_status = 'WARN'` |
| `computed_at` | TIMESTAMP | Stage 4 run timestamp |

---

### `stage_compute.per_sale_bonus`

One row per sale. Adds bonus eligibility flag and amount earned.  
**Granularity:** `sale_id`

| Column | Type | Description |
|---|---|---|
| `sale_id` | VARCHAR | Sale identifier |
| `sale_date` | DATE | Sale date |
| `merchant_id` | VARCHAR | Merchant |
| `merchant_name` | VARCHAR | |
| `store_id` | VARCHAR | Store |
| `store_name` | VARCHAR | |
| `customer_id` | VARCHAR | Customer |
| `customer_name` | VARCHAR | |
| `product_id` | VARCHAR | Product |
| `product_name` | VARCHAR | |
| `quantity` | INTEGER | Units sold |
| `unit_price` | DECIMAL(10,2) | Unit price |
| `total_amount` | DECIMAL(10,2) | Sale total |
| `bonus_threshold` | INTEGER | Minimum sale amount for bonus |
| `bonus_amount` | DECIMAL(10,2) | Bonus payable if eligible |
| `is_bonus_eligible` | BOOLEAN | `total_amount >= bonus_threshold` |
| `bonus_earned` | DECIMAL(10,2) | `bonus_amount` if eligible, else `0` |
| `computed_at` | TIMESTAMP | |

**Eligibility rule:**  
A sale qualifies for a bonus when `total_amount >= bonus_threshold` and `bonus_threshold IS NOT NULL`.

---

### `stage_compute.merchant_bonus`

Bonus aggregates per merchant store per day.  
**Granularity:** `(merchant_id, store_id, sale_date)`

| Column | Type | Description |
|---|---|---|
| `merchant_id` | VARCHAR | |
| `merchant_name` | VARCHAR | |
| `store_id` | VARCHAR | |
| `store_name` | VARCHAR | |
| `sale_date` | DATE | |
| `total_sales` | BIGINT | Total sales on this day |
| `eligible_sales` | BIGINT | Bonus-eligible sales |
| `total_bonus_earned` | DOUBLE | Sum of `bonus_earned` |
| `total_revenue` | DOUBLE | Sum of `total_amount` |
| `bonus_hit_rate_pct` | DOUBLE | `eligible_sales / total_sales × 100` |
| `computed_at` | TIMESTAMP | |

---

### `stage_compute.customer_metrics`

Lifetime purchase behaviour per customer.  
**Granularity:** `customer_id`

| Column | Type | Description |
|---|---|---|
| `customer_id` | VARCHAR | Customer identifier |
| `customer_name` | VARCHAR | |
| `customer_email` | VARCHAR | |
| `customer_country` | VARCHAR | |
| `registration_date` | DATE | |
| `first_purchase_date` | DATE | Earliest `sale_date` |
| `last_purchase_date` | DATE | Most recent `sale_date` |
| `total_transactions` | BIGINT | |
| `total_units_bought` | BIGINT | |
| `total_spend` | DOUBLE | Sum of `total_amount` |
| `avg_order_value` | DOUBLE | |
| `stores_visited` | BIGINT | Distinct stores |
| `distinct_products_bought` | BIGINT | |
| `purchase_span_days` | INTEGER | `last_purchase_date − first_purchase_date` |
| `computed_at` | TIMESTAMP | |

---

### `stage_compute.churn_flags`

One row per customer with inactivity-based churn flag.  
**Granularity:** `customer_id`

| Column | Type | Description |
|---|---|---|
| `customer_id` | VARCHAR | |
| `customer_name` | VARCHAR | |
| `customer_email` | VARCHAR | |
| `customer_country` | VARCHAR | |
| `first_purchase_date` | DATE | |
| `last_purchase_date` | DATE | |
| `total_transactions` | BIGINT | |
| `total_spend` | DOUBLE | |
| `reference_date` | DATE | `MAX(sale_date)` across all integrated sales |
| `days_since_last_purchase` | INTEGER | `reference_date − last_purchase_date` |
| `inactivity_threshold_days` | INTEGER | Threshold used in this run |
| `is_churned` | BOOLEAN | `days_since_last_purchase > inactivity_threshold_days` |
| `computed_at` | TIMESTAMP | |

**Churn logic:**  
`is_churned = (reference_date − last_purchase_date) > inactivity_threshold_days`

The `reference_date` is always the latest sale date present in the data —
not today's calendar date — so the flag is reproducible regardless of when
Stage 4 is run.

---

## Useful queries

```sql
-- Daily revenue trend across all stores
SELECT sale_date, SUM(total_revenue) AS revenue, SUM(transaction_count) AS txns
FROM   stage_compute.sales_kpis
GROUP  BY sale_date
ORDER  BY sale_date;

-- Stores ranked by total bonus earned
SELECT merchant_name, store_name,
       SUM(total_bonus_earned)  AS bonus,
       SUM(total_revenue)       AS revenue,
       ROUND(SUM(total_bonus_earned) / SUM(total_revenue) * 100, 2) AS bonus_pct
FROM   stage_compute.merchant_bonus
GROUP  BY merchant_name, store_name
ORDER  BY bonus DESC;

-- Customers at risk of churn by spend
SELECT customer_name, customer_email, last_purchase_date,
       days_since_last_purchase, total_spend
FROM   stage_compute.churn_flags
WHERE  is_churned
ORDER  BY total_spend DESC;

-- Products with highest bonus capture rate
SELECT product_name,
       COUNT(*)                                               AS sales,
       SUM(CASE WHEN is_bonus_eligible THEN 1 ELSE 0 END)    AS eligible,
       SUM(bonus_earned)                                      AS total_bonus
FROM   stage_compute.per_sale_bonus
GROUP  BY product_name
ORDER  BY total_bonus DESC;
```

---

## Logging

```
logs/stage4_YYYY-MM-DD_HHMMSS.log
```

| Level | When |
|---|---|
| `INFO` | Row counts per output table; churn summary |
| `ERROR` | Database not found; SQL failure |

---

## Re-running

Stage 4 is a full reprocess — simply run it again:

```bash
python stage4.py
```

To change the churn window without rerunning Stages 1–3:

```bash
python stage4.py --inactivity-days 30
python stage5.py                        # re-render reports
```

---

## Assumptions

1. **Only FULL joins are computed.** Rows with `join_status = 'PARTIAL'` are
   excluded. This ensures no NULL merchant, customer, or bonus data enters any
   aggregate. PARTIAL counts are visible in `stage_integrate.integrated_sales`.

2. **Churn reference date is data-relative.** The reference date is
   `MAX(sale_date)` from the data, not today's system date. Re-running Stage 4
   on a later calendar date without loading new data will not change which
   customers are churned.

3. **Bonus eligibility is sale-level.** Each sale is assessed independently
   against the bonus tier active on its `sale_date`. There is no cumulative
   or period-level threshold.

4. **Multiple SCD versions.** If `per_sale_bonus` or `per_sale_bonus` contains
   two rows for the same `product_id` on the same day (overlapping SCD records),
   this is inherited from Stage 3's deduplication rule (latest `valid_from`
   wins). See Stage 1 and Stage 3 documentation.

5. **`customer_metrics` takes the latest dimension attribute.** When a customer
   changed their email or country across SCD records, `MAX(customer_email)` and
   `MAX(customer_country)` are used — this is alphabetically last, not
   necessarily the most recent. For strict point-in-time accuracy, join
   `stage_integrate.integrated_sales` directly.
