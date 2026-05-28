# Stage 5 — Report README

**Pipeline:** [README](README.md) — [Stage 1 (Ingest)](INGEST_README.md) →
[Stage 2 (Cleanup)](STAGE2_README.md) → [Stage 3 (Integrate)](STAGE3_README.md) →
[Stage 4 (Compute)](STAGE4_README.md) → Stage 5 (Report)

Reads the analytical tables from `stage_compute` (Stage 4) and renders a set
of linked HTML files into the `reports/` directory.

    reports/
    ├── index.html                  ← dashboard: KPI summary + navigation
    ├── sales_summary.html          ← overall revenue and volume by day and store
    ├── bonus_report.html           ← bonus eligibility and merchant earnings
    ├── churn_report.html           ← customer churn classification
    └── merchant_<id>.html          ← one page per merchant / store

Full reprocess: the `reports/` directory is cleared and rebuilt on every run.

---

## Prerequisites

Stages 1–4 must have run successfully.

```bash
python pipeline.py --date YYYY-MM-DD   # Stage 1
python stage2.py                       # Stage 2
python stage3.py                       # Stage 3
python stage4.py                       # Stage 4
```

No additional Python packages are required beyond `duckdb` (already installed).

---

## Usage

```bash
python stage5.py                              # write to reports/ (default)
python stage5.py --output-dir /tmp/my_report  # custom output directory
```

| Argument | Default | Meaning |
|---|---|---|
| `--output-dir` | `reports/` relative to the project root | Directory where HTML files are written. Created automatically; existing contents are deleted on each run. |

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | All HTML files written successfully |
| `1` | Fatal error (database not found, Stage 4 tables missing, write error) |

---

## Report pages

### `index.html` — Dashboard

The entry point. Contains:
- **Pipeline KPI bar** — total revenue, transactions, stores, bonus earned,
  churned vs. total customers
- **Report navigation cards** — links to Sales Summary, Bonus Report, Churn
  Report
- **Merchant cards** — one card per merchant with revenue, transaction count,
  and a direct link to the per-merchant report page

### `sales_summary.html` — Sales Summary

- **Daily sales trend** table — by day: stores, transactions, units, customers,
  revenue, average order value
- **Store performance** table — aggregated across all days: revenue, units,
  transactions per store, warn row count
- **Top 10 products** by revenue — product ID, name, transactions, units,
  revenue, average unit price

### `bonus_report.html` — Bonus Report

- **Merchant bonus summary** — total sales, eligible sales, bonus earned, hit
  rate per merchant (all days combined)
- **Daily bonus by merchant** — eligible sales and bonus earned per
  merchant per day
- **Sale-level detail** — top 50 sales by bonus earned, with eligibility badge

A sale is bonus-eligible when `total_amount >= bonus_threshold`.

### `churn_report.html` — Customer Churn

- **Churn summary KPIs** — reference date, inactivity threshold, total customers,
  churned count, active count, revenue at risk (total spend of churned customers)
- **Churned customers** table — sorted by total spend (highest revenue at risk first)
- **Active customers** table — sorted by last purchase date

The inactivity threshold comes from the value used in the last Stage 4 run
(stored in the `churn_flags.inactivity_threshold_days` column).

### `merchant_<id>.html` — Per-Merchant Report

One page per merchant. Contains:
- **KPI bar** — total revenue, transactions, customers, bonus earned, hit rate
- **Daily sales** table — revenue and volume by day
- **Product breakdown** — revenue, units, bonus earned per product
- **Customer list** — all customers who purchased from this store, with spend,
  visit frequency, and Active/Churned badge

---

## Styling

All CSS is embedded directly in each HTML file — no external stylesheets,
fonts, or JavaScript libraries are required. The reports are self-contained
and can be opened in any browser or attached to an email.

Colour conventions used in the reports:

| Colour | Meaning |
|---|---|
| Green badge | Active customer / bonus-eligible sale |
| Amber | Warning (data quality flag) |
| Red badge | Churned customer |
| Blue header | Report / table section heading |

---

## Logging

```
logs/stage5_YYYY-MM-DD_HHMMSS.log
```

| Level | When |
|---|---|
| `INFO` | Each file written; final file count summary |
| `ERROR` | Database not found; Stage 4 tables missing; write failure |

---

## Re-running

Stage 5 is a full reprocess:

```bash
python stage5.py
```

The `reports/` directory is cleared and all files are regenerated.
To regenerate with a different churn window, re-run Stage 4 first:

```bash
python stage4.py --inactivity-days 30
python stage5.py
```

To write to a different location (e.g. a web server document root):

```bash
python stage5.py --output-dir /var/www/html/sales-reports
```

---

## Assumptions

1. **Stage 4 must complete before Stage 5.** Stage 5 performs a pre-flight
   check for all five required `stage_compute` tables; if any are missing it
   exits with code 1.

2. **Stage 5 opens the database read-only.** It does not write to DuckDB.
   The database can be queried from another tool (e.g. DBeaver) simultaneously
   while Stage 5 runs.

3. **One HTML file per merchant ID.** Merchant IDs must be valid filename
   components. If a merchant ID contains characters not safe for file names
   (e.g. `/`, `\`), the file will fail to write. In practice IDs follow the
   `M001` pattern used in the source data.

4. **All monetary values are displayed with `€` prefix.** The currency symbol
   is cosmetic and does not reflect a DuckDB currency type. Change `_fmt_currency`
   in `stage5.py` if a different symbol is required.

5. **The churn threshold in the report reflects the last Stage 4 run.** The
   `inactivity_threshold_days` column in `stage_compute.churn_flags` is read
   directly; Stage 5 does not apply its own threshold logic.
