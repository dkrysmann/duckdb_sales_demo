# Sales Pipeline

A five-stage data pipeline that ingests daily CSV partitions, cleans and validates
them, joins dimensions with full SCD Type 2 history, computes sales KPIs and customer
analytics, and renders a set of self-contained HTML reports.

All data lives in a single **DuckDB** file (`stages.duckdb`) partitioned into one
schema per stage. No external database server is required.

---

## Pipeline Overview

```
 CSV files                stages.duckdb                         reports/
 (daily partitions)       ┌──────────────────┐
                          │  stage_ingest     │
 data/                    │  ─────────────    │
 ├── product_sales/  ──►  │  product_sales    │
 ├── merchants/      ──►  │  merchants        │  Stage 1 — Ingest
 ├── customers/      ──►  │  customers        │  (pipeline.py)
 └── bonus_tiers/    ──►  │  bonus_tiers      │
                          │  _ingest_metadata │
                          │                  │
                          │  stage_cleanup    │
                          │  ─────────────    │
                          │  clean_product_   │  Stage 2 — Cleanup
                          │    sales          │  (stage2.py)
                          │  clean_merchants  │
                          │  clean_customers  │
                          │  clean_bonus_     │
                          │    tiers          │
                          │                  │
                          │  stage_integrate  │
                          │  ─────────────    │  Stage 3 — Integrate
                          │  integrated_sales │  (stage3.py)
                          │                  │
                          │  stage_compute    │
                          │  ─────────────    │
                          │  sales_kpis       │  Stage 4 — Compute
                          │  per_sale_bonus   │  (stage4.py)
                          │  merchant_bonus   │
                          │  customer_metrics │
                          │  churn_flags      │
                          └──────────────────┘
                                   │
                                   ▼           Stage 5 — Report
                          reports/             (stage5.py)
                          ├── index.html
                          ├── sales_summary.html
                          ├── bonus_report.html
                          ├── churn_report.html
                          └── merchant_M*.html
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

Requires **Python 3.14+**.

### 2. Run the full pipeline

```bash
# Stage 1 — ingest one day's data (repeat for each available date)
python pipeline.py --date 2026-05-25
python pipeline.py --date 2026-05-26
python pipeline.py --date 2026-05-27

# Stage 2 — clean & validate (processes all ingested data)
python stage2.py

# Stage 3 — integrate dimensions into a wide fact table
python stage3.py

# Stage 4 — compute KPIs and customer analytics
python stage4.py

# Stage 5 — render HTML reports
python stage5.py
```

Open `reports/index.html` in any browser to view the dashboard.

### 3. Sample data

Three days of test data are included under `data/` (2026-05-25 through 2026-05-27).
Run the Stage 1 commands above in order to load them.

---

## Stages at a Glance

| Stage | Script | Input | Output | Key feature |
|---|---|---|---|---|
| **1 — Ingest** | `pipeline.py` | CSV partitions | `stage_ingest` | SCD Type 2 for dimensions; append for facts; idempotent |
| **2 — Cleanup** | `stage2.py` | `stage_ingest` | `stage_cleanup` | Country fuzzy-matching; FK validation; PASS / WARN / FAIL status |
| **3 — Integrate** | `stage3.py` | `stage_cleanup` | `stage_integrate` | SCD-aware dimension joins; `join_status` = FULL / PARTIAL |
| **4 — Compute** | `stage4.py` | `stage_integrate` | `stage_compute` | Sales KPIs, bonus eligibility, customer lifetime metrics, churn flags |
| **5 — Report** | `stage5.py` | `stage_compute` | `reports/*.html` | Self-contained HTML; no external CSS or JS |

---

## Stage Details

### Stage 1 — Ingest (`pipeline.py`)
→ [INGEST_README.md](INGEST_README.md)

Loads daily CSV files into the `stage_ingest` schema.

- **Facts** (`product_sales`) — appended each run; duplicate dates are skipped
  (idempotent via `_ingest_metadata` tracking table).
- **Dimensions** (`merchants`, `customers`, `bonus_tiers`) — managed with
  **SCD Type 2**: new versions are inserted when attributes change; old versions
  are closed with a `valid_to` date.
- Missing files are skipped with a warning; the run does not abort.
- Schema drift is detected and logged with suggested `ALTER TABLE` statements.

```bash
python pipeline.py --date YYYY-MM-DD
```

---

### Stage 2 — Cleanup (`stage2.py`)
→ [STAGE2_README.md](STAGE2_README.md)

Validates and normalises the raw tables. Produces four `clean_*` tables in
the `stage_cleanup` schema.

**Validations performed:**

| Check type | Examples |
|---|---|
| Structural NULLs | `sale_id IS NULL`, `customer_id IS NULL` |
| Business rules | `unit_price <= 0`, `bonus_threshold <= 0` |
| Referential integrity | FK store / customer checks (SCD-aware) |
| Format checks | Email pattern, country encoding |

**Country normalisation** uses a four-step process: strip special characters →
collapse whitespace → alias lookup (e.g. `uk` → `United Kingdom`) → fuzzy match
against ISO 3166-1 names via `rapidfuzz`.

Every row receives a `validation_status` of `PASS`, `WARN`, or `FAIL`, and a
pipe-separated `validation_reasons` column listing any failed check codes.

```bash
python stage2.py                   # default fuzzy threshold = 80
python stage2.py --threshold 75    # accept weaker country matches
```

---

### Stage 3 — Integrate (`stage3.py`)
→ [STAGE3_README.md](STAGE3_README.md)

Joins the clean sales fact table with all three dimension tables into one wide
fact table: `stage_integrate.integrated_sales`.

- **SCD-aware joins** — for each sale, resolves the dimension record whose
  `valid_from ≤ sale_date AND (valid_to > sale_date OR valid_to IS NULL)`.
- When multiple SCD versions overlap on the same date, the latest `valid_from`
  wins (implemented with `ROW_NUMBER()`).
- Rows with `validation_status = 'FAIL'` are excluded from both the fact
  table and the dimension lookup.
- LEFT JOIN strategy: a sale missing any dimension is retained with NULL columns
  and `join_status = 'PARTIAL'` so coverage gaps are visible downstream.

```bash
python stage3.py
```

---

### Stage 4 — Compute (`stage4.py`)
→ [STAGE4_README.md](STAGE4_README.md)

Reads `stage_integrate.integrated_sales` (FULL joins only) and builds five
analytical tables in the `stage_compute` schema.

| Table | Granularity | Description |
|---|---|---|
| `sales_kpis` | merchant + store + day | Revenue, volume, and order-value KPIs |
| `per_sale_bonus` | sale | Per-sale bonus eligibility (`total_amount >= bonus_threshold`) |
| `merchant_bonus` | merchant + store + day | Bonus hit rate and total bonus earned |
| `customer_metrics` | customer | Lifetime spend, visit frequency, purchase span |
| `churn_flags` | customer | Inactivity-based churn flag; reference date = `MAX(sale_date)` in data |

The churn reference date is **data-relative**, not calendar-relative, so
re-running Stage 4 later without loading new data produces identical results.

```bash
python stage4.py                       # inactivity window = 1 day (default)
python stage4.py --inactivity-days 30  # flag customers silent > 30 days
```

---

### Stage 5 — Report (`stage5.py`)
→ [STAGE5_README.md](STAGE5_README.md)

Reads the `stage_compute` tables (read-only connection — DBeaver can be open
simultaneously) and renders HTML reports into `reports/`.

| File | Contents |
|---|---|
| `index.html` | Pipeline KPI bar, merchant cards, navigation |
| `sales_summary.html` | Daily trend, store performance, top 10 products |
| `bonus_report.html` | Merchant bonus summary, daily breakdown, sale-level detail |
| `churn_report.html` | Churn KPIs, churned/active customer tables |
| `merchant_M*.html` | Per-merchant: daily sales, product breakdown, customer list |

All CSS is embedded inline — reports are fully self-contained and can be opened
offline or attached to an email.

```bash
python stage5.py                              # writes to reports/
python stage5.py --output-dir /tmp/my_report  # custom output directory
```

---

## Project Layout

```
assesement_opp/
├── pipeline.py                  ← Stage 1: ingest
├── stage2.py                    ← Stage 2: cleanup & validation
├── stage3.py                    ← Stage 3: integration
├── stage4.py                    ← Stage 4: compute
├── stage5.py                    ← Stage 5: report
├── shelly_streaming_producer.py ← Shelly IoT → Snowflake batch producer
│
├── requirements.txt     ← Python dependencies
├── stages.duckdb        ← Single DuckDB file (all schemas)
│
├── data/                ← Source data (date-partitioned CSVs)
│   ├── product_sales/YYYY/MM/DD/product_sales.csv
│   ├── merchants/YYYY/MM/DD/merchants.csv
│   ├── customers/YYYY/MM/DD/customers.csv
│   └── bonus_tiers/YYYY/MM/DD/bonus_tiers.csv
│
├── reports/             ← Generated HTML reports (Stage 5 output)
│   ├── index.html
│   ├── sales_summary.html
│   ├── bonus_report.html
│   ├── churn_report.html
│   └── merchant_M*.html
│
├── logs/                ← One log file per stage run
│   ├── ingest_YYYY-MM-DD.log
│   ├── stage2_YYYY-MM-DD_HHMMSS.log
│   ├── stage3_YYYY-MM-DD_HHMMSS.log
│   ├── stage4_YYYY-MM-DD_HHMMSS.log
│   └── stage5_YYYY-MM-DD_HHMMSS.log
│
├── terraform/
│   └── s3-restricted/   ← AWS S3 + Snowflake integration Terraform
│
├── INGEST_README.md     ← Stage 1 detailed documentation
├── STAGE2_README.md     ← Stage 2 detailed documentation
├── STAGE3_README.md     ← Stage 3 detailed documentation
├── STAGE4_README.md     ← Stage 4 detailed documentation
├── STAGE5_README.md     ← Stage 5 detailed documentation
└── SNOWFLAKE_README.md  ← Snowflake integration documentation
```

---

## Dependencies

| Package | Version | Used by |
|---|---|---|
| `duckdb` | 1.5.3 | Stages 1–5 |
| `pycountry` | 26.2.16 | Stage 2 — ISO 3166-1 country list |
| `rapidfuzz` | 3.14.5 | Stage 2 — fuzzy country matching |
| `openpyxl` | 3.1.5 | Stage 2 — Excel compatibility |
| `snowflake-connector-python` | ≥ 3.0.0 | Shelly producer — Snowflake inserts |
| `boto3` | ≥ 1.34.0 | Shelly producer — S3 reads |
| `cryptography` | ≥ 42.0.0 | Shelly producer — RSA key loading |

```bash
pip install -r requirements.txt
```

---

## Data Flow and Row Filtering

```
Stage 1 — all rows ingested (append)
    │
    ▼
Stage 2 — all rows retained, tagged PASS / WARN / FAIL
    │         FAIL rows excluded from Stage 3 joins
    ▼
Stage 3 — PASS + WARN sales joined against clean dimensions
    │         join_status = FULL  (all 3 dims resolved)
    │         join_status = PARTIAL (≥ 1 dim missing → NULL columns)
    ▼
Stage 4 — only join_status = FULL rows aggregated
    │         PARTIAL rows visible in stage_integrate but not in stage_compute
    ▼
Stage 5 — reads stage_compute tables (read-only)
              renders HTML reports
```

A row that fails a check is **never silently dropped** — it is retained in each
schema with an explicit status column so the gap is always visible.

---

## Re-running Individual Stages

Each stage except Stage 1 is a **full reprocess** — drop and rebuild all output
tables or files from the current upstream state.

| Scenario | Commands |
|---|---|
| Load a new day's data | `python pipeline.py --date YYYY-MM-DD` then re-run Stages 2–5 |
| Re-clean after threshold change | `python stage2.py --threshold N` then re-run Stages 3–5 |
| Re-integrate after source fix | `python stage3.py` then re-run Stages 4–5 |
| Change churn window | `python stage4.py --inactivity-days N` then `python stage5.py` |
| Regenerate reports only | `python stage5.py` |

---

## Logging

Every stage writes to `logs/`. Stage 1 appends to a per-date file; Stages 2–5
create a new timestamped file per run so no run overwrites another.

```
logs/
├── ingest_2026-05-27.log                  ← Stage 1 (appended per date)
├── stage2_2026-05-27_192340.log           ← Stage 2 (new file per run)
├── stage3_2026-05-27_193559.log           ← Stage 3
├── stage4_2026-05-27_200254.log           ← Stage 4
└── stage5_2026-05-27_200330.log           ← Stage 5
```

---

## Terraform — AWS S3 + Snowflake Deployment

A Terraform configuration is included under `terraform/s3-restricted/` for deploying
a production-grade S3 bucket with restricted IAM access and a full Snowflake integration.

→ [terraform/s3-restricted/README.md](terraform/s3-restricted/README.md)  
→ [SNOWFLAKE_README.md](SNOWFLAKE_README.md) — Snowflake resources, producer setup, troubleshooting

**S3 features:**
- Two IAM roles: **reader** (`GetObject`, `ListBucket`, …) and **writer**
  (read + `PutObject`, `DeleteObject`, …)
- Bucket policy: `Effect: Deny + ArnNotLike` — overrides any identity-policy
  `Allow`, preventing privilege escalation via user policies
- AES-256 server-side encryption, public access block (all 4 flags), optional
  versioning and access logging

**Snowflake features:**
- Storage integration (`S3_INTEGRATION`) + external stages for both S3 buckets
- `CONTRACTS` table with schema inferred from HubSpot JSON; auto-ingest via Snowpipe
- `SHELLY_PWR` table (VARIANT schema) loaded by `shelly_streaming_producer.py`
- Dedicated service account `SHELLY_STREAMER` with least-privilege role

```bash
cd terraform/s3-restricted
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars — see SNOWFLAKE_README.md for required variables
export TF_VAR_snowflake_external_id="..."
unset SNOWFLAKE_PRIVATE_KEY_PATH   # must not be set during terraform runs
terraform init && terraform apply
```
