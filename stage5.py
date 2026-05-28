"""
stage5.py — Stage 5: Report

Reads stage_compute tables (Stage 4) and renders a set of HTML reports
into the reports/ directory.

    reports/
    ├── index.html                  ← main page: pipeline summary + links
    ├── sales_summary.html          ← overall revenue / volume across all stores
    ├── bonus_report.html           ← bonus eligibility and earnings
    ├── churn_report.html           ← customer churn classification
    └── merchant_<id>.html          ← one page per merchant

Full reprocess: the reports/ directory is cleared and rebuilt on every run.

Usage:
    python stage5.py
    python stage5.py --output-dir /path/to/reports

Exit codes:
    0  All reports written successfully
    1  Fatal error (database not found, compute tables missing)
"""

from __future__ import annotations

import argparse
import html as html_mod
import logging
import shutil
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT_DIR      = Path(__file__).parent
DB_PATH       = ROOT_DIR / "stages.duckdb"
LOG_DIR       = ROOT_DIR / "logs"
DEFAULT_OUT   = ROOT_DIR / "reports"
SCHEMA        = "stage_compute"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    run_ts   = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = LOG_DIR / f"stage5_{run_ts}.log"

    logger = logging.getLogger("stage5")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fh  = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh  = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger

# ---------------------------------------------------------------------------
# HTML primitives
# ---------------------------------------------------------------------------

_CSS = """
:root {
    --brand:   #1a3a5c;
    --accent:  #2e86c1;
    --light:   #eaf4fb;
    --pass:    #1e8449;
    --warn:    #d68910;
    --fail:    #c0392b;
    --muted:   #7f8c8d;
    --border:  #d5d8dc;
    --bg:      #f8f9fa;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: 'Segoe UI', Arial, sans-serif;
    font-size: 14px;
    background: var(--bg);
    color: #2c3e50;
    line-height: 1.5;
}
header {
    background: var(--brand);
    color: #fff;
    padding: 16px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
header h1 { font-size: 1.3rem; font-weight: 600; }
header .sub { font-size: 0.8rem; opacity: 0.7; margin-top: 2px; }
nav.breadcrumb {
    background: var(--light);
    border-bottom: 1px solid var(--border);
    padding: 8px 32px;
    font-size: 0.82rem;
    color: var(--muted);
}
nav.breadcrumb a { color: var(--accent); text-decoration: none; }
nav.breadcrumb a:hover { text-decoration: underline; }
main { padding: 24px 32px; max-width: 1280px; }
h2 { font-size: 1.1rem; font-weight: 600; color: var(--brand);
     margin: 24px 0 12px; border-bottom: 2px solid var(--accent);
     padding-bottom: 4px; }
h3 { font-size: 0.95rem; font-weight: 600; color: var(--brand); margin: 20px 0 8px; }
.kpi-row {
    display: flex; gap: 16px; flex-wrap: wrap; margin: 16px 0 24px;
}
.kpi {
    background: #fff; border: 1px solid var(--border); border-radius: 6px;
    padding: 16px 24px; min-width: 160px; flex: 1;
}
.kpi .val { font-size: 1.6rem; font-weight: 700; color: var(--brand); }
.kpi .lbl { font-size: 0.78rem; color: var(--muted); margin-top: 2px; }
table {
    width: 100%; border-collapse: collapse;
    background: #fff; border: 1px solid var(--border);
    border-radius: 6px; overflow: hidden; margin-bottom: 24px;
    font-size: 0.88rem;
}
thead th {
    background: var(--brand); color: #fff;
    padding: 9px 12px; text-align: left; font-weight: 600;
}
tbody tr:nth-child(even) { background: var(--light); }
tbody tr:hover { background: #d6eaf8; }
td, tbody th { padding: 8px 12px; border-bottom: 1px solid var(--border); }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.badge {
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 0.75rem; font-weight: 600;
}
.badge-pass  { background: #d5f5e3; color: var(--pass); }
.badge-warn  { background: #fdebd0; color: var(--warn); }
.badge-fail  { background: #fadbd8; color: var(--fail); }
.badge-churn { background: #fadbd8; color: var(--fail); }
.badge-active{ background: #d5f5e3; color: var(--pass); }
.card-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 16px; margin: 16px 0 28px;
}
.card {
    background: #fff; border: 1px solid var(--border); border-radius: 6px;
    padding: 16px; text-decoration: none; color: inherit;
    transition: box-shadow .15s;
}
.card:hover { box-shadow: 0 4px 12px rgba(0,0,0,.12); }
.card .card-title { font-weight: 600; color: var(--accent); margin-bottom: 6px; }
.card .card-meta  { font-size: 0.78rem; color: var(--muted); }
footer {
    margin-top: 40px; padding: 16px 32px;
    border-top: 1px solid var(--border); font-size: 0.78rem; color: var(--muted);
}
"""


def _e(value: Any) -> str:
    """HTML-escape a value for safe output."""
    return html_mod.escape(str(value)) if value is not None else "—"


def _fmt_currency(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"€{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _fmt_int(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _page(title: str, breadcrumb: str, body: str,
          generated_at: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <div>
    <div class="sub">Sales Pipeline Report</div>
    <h1>{_e(title)}</h1>
  </div>
  <div style="text-align:right">
    <div class="sub">Generated</div>
    <div style="font-size:.85rem">{_e(generated_at)}</div>
  </div>
</header>
<nav class="breadcrumb">{breadcrumb}</nav>
<main>
{body}
</main>
<footer>Generated by stage5.py &nbsp;|&nbsp; {_e(generated_at)}</footer>
</body>
</html>"""


def _table(headers: list[str], rows: list[tuple],
           formatters: list | None = None) -> str:
    th_html = "".join(f"<th>{_e(h)}</th>" for h in headers)
    fmts = formatters or [None] * len(headers)
    body_rows = []
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            fmt = fmts[i] if i < len(fmts) else None
            if fmt:
                val = fmt(cell)
                cls = ' class="num"' if fmt in (
                    _fmt_currency, _fmt_pct, _fmt_int) else ""
                cells.append(f"<td{cls}>{val}</td>")
            else:
                cells.append(f"<td>{_e(cell)}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<table>"
        f"<thead><tr>{th_html}</tr></thead>"
        "<tbody>" + "".join(body_rows) + "</tbody>"
        "</table>"
    )


def _kpi_row(*items: tuple[str, str]) -> str:
    cards = "".join(
        f'<div class="kpi"><div class="val">{v}</div>'
        f'<div class="lbl">{_e(l)}</div></div>'
        for v, l in items
    )
    return f'<div class="kpi-row">{cards}</div>'

# ---------------------------------------------------------------------------
# Report: index.html
# ---------------------------------------------------------------------------

def build_index(con: duckdb.DuckDBPyConnection,
                out_dir: Path,
                generated_at: str,
                logger: logging.Logger) -> None:

    # Top-level KPIs
    kpi = con.execute(f"""
        SELECT
            COUNT(DISTINCT store_id)        AS stores,
            SUM(total_revenue)              AS revenue,
            SUM(transaction_count)          AS transactions,
            SUM(unique_customers)           AS cust_days
        FROM {SCHEMA}.sales_kpis
    """).fetchone()

    bonus_total = con.execute(f"""
        SELECT SUM(bonus_earned) FROM {SCHEMA}.per_sale_bonus
    """).fetchone()[0] or 0

    churn_count = con.execute(f"""
        SELECT COUNT(*) FROM {SCHEMA}.churn_flags WHERE is_churned
    """).fetchone()[0]

    total_customers = con.execute(f"""
        SELECT COUNT(*) FROM {SCHEMA}.customer_metrics
    """).fetchone()[0]

    kpis = _kpi_row(
        (_fmt_currency(kpi[1]), "Total Revenue"),
        (_fmt_int(kpi[2]),      "Total Transactions"),
        (_fmt_int(kpi[0]),      "Active Stores"),
        (_fmt_currency(bonus_total), "Total Bonus Earned"),
        (f"{churn_count} / {total_customers}", "Churned / Total Customers"),
    )

    # Merchant cards — link to per-merchant pages
    merchants = con.execute(f"""
        SELECT
            merchant_id,
            MAX(merchant_name)  AS merchant_name,
            MAX(store_name)     AS store_name,
            MAX(merchant_country) AS country,
            SUM(total_revenue)  AS revenue,
            SUM(transaction_count) AS txns
        FROM {SCHEMA}.sales_kpis
        GROUP BY merchant_id
        ORDER BY revenue DESC
    """).fetchall()

    cards = ""
    for m in merchants:
        mid, mname, sname, country, rev, txns = m
        safe_id = _e(mid)
        cards += f"""
        <a class="card" href="merchant_{html_mod.escape(mid)}.html">
          <div class="card-title">{_e(mname)} — {_e(sname)}</div>
          <div class="card-meta">
            {_e(country)}&nbsp;&nbsp;|&nbsp;&nbsp;
            {_fmt_currency(rev)} revenue&nbsp;&nbsp;|&nbsp;&nbsp;
            {_fmt_int(txns)} transactions
          </div>
        </a>"""

    # Report link cards
    report_links = """
    <h2>Reports</h2>
    <div class="card-grid">
      <a class="card" href="sales_summary.html">
        <div class="card-title">📊 Sales Summary</div>
        <div class="card-meta">Overall revenue and volume across all stores by day</div>
      </a>
      <a class="card" href="bonus_report.html">
        <div class="card-title">🎯 Bonus Report</div>
        <div class="card-meta">Bonus eligibility per sale and aggregate earnings per merchant</div>
      </a>
      <a class="card" href="churn_report.html">
        <div class="card-title">⚠️ Customer Churn</div>
        <div class="card-meta">Customers classified by inactivity window</div>
      </a>
    </div>"""

    body = (
        "<h2>Pipeline Overview</h2>"
        + kpis
        + report_links
        + "<h2>Merchants</h2>"
        + '<div class="card-grid">' + cards + "</div>"
    )

    path = out_dir / "index.html"
    path.write_text(
        _page("Sales Pipeline — Dashboard",
              '<a href="index.html">Home</a>',
              body, generated_at),
        encoding="utf-8",
    )
    logger.info("  Written: %s", path)

# ---------------------------------------------------------------------------
# Report: sales_summary.html
# ---------------------------------------------------------------------------

def build_sales_summary(con: duckdb.DuckDBPyConnection,
                        out_dir: Path,
                        generated_at: str,
                        logger: logging.Logger) -> None:

    # By day
    by_day = con.execute(f"""
        SELECT
            sale_date,
            COUNT(DISTINCT store_id)    AS stores,
            SUM(transaction_count)      AS transactions,
            SUM(units_sold)             AS units,
            SUM(unique_customers)       AS customers,
            SUM(total_revenue)          AS revenue,
            AVG(avg_order_value)        AS avg_order
        FROM {SCHEMA}.sales_kpis
        GROUP BY sale_date
        ORDER BY sale_date
    """).fetchall()

    # By store (all days combined)
    by_store = con.execute(f"""
        SELECT
            merchant_name,
            store_name,
            merchant_country,
            SUM(transaction_count)  AS transactions,
            SUM(units_sold)         AS units,
            SUM(total_revenue)      AS revenue,
            AVG(avg_order_value)    AS avg_order,
            SUM(warn_row_count)     AS warn_rows
        FROM {SCHEMA}.sales_kpis
        GROUP BY merchant_name, store_name, merchant_country
        ORDER BY revenue DESC
    """).fetchall()

    # Top 10 products by revenue from per_sale_bonus (has product detail)
    by_product = con.execute(f"""
        SELECT
            product_id,
            MAX(product_name)       AS product_name,
            COUNT(*)                AS transactions,
            SUM(quantity)           AS units_sold,
            SUM(total_amount)       AS revenue,
            AVG(unit_price)         AS avg_price
        FROM {SCHEMA}.per_sale_bonus
        GROUP BY product_id
        ORDER BY revenue DESC
        LIMIT 10
    """).fetchall()

    day_tbl = _table(
        ["Sale Date", "Stores", "Transactions", "Units Sold",
         "Unique Customers", "Total Revenue", "Avg Order Value"],
        by_day,
        [_e, _fmt_int, _fmt_int, _fmt_int, _fmt_int,
         _fmt_currency, _fmt_currency],
    )

    store_tbl = _table(
        ["Merchant", "Store", "Country", "Transactions", "Units",
         "Total Revenue", "Avg Order", "Warn Rows"],
        by_store,
        [_e, _e, _e, _fmt_int, _fmt_int,
         _fmt_currency, _fmt_currency, _fmt_int],
    )

    prod_tbl = _table(
        ["Product ID", "Product Name", "Transactions", "Units Sold",
         "Revenue", "Avg Unit Price"],
        by_product,
        [_e, _e, _fmt_int, _fmt_int, _fmt_currency, _fmt_currency],
    )

    body = (
        "<h2>Daily Sales Trend</h2>" + day_tbl
        + "<h2>Store Performance (All Days)</h2>" + store_tbl
        + "<h2>Top Products by Revenue</h2>" + prod_tbl
    )

    crumb = '<a href="index.html">Home</a> › Sales Summary'
    path  = out_dir / "sales_summary.html"
    path.write_text(
        _page("Sales Summary", crumb, body, generated_at),
        encoding="utf-8",
    )
    logger.info("  Written: %s", path)

# ---------------------------------------------------------------------------
# Report: bonus_report.html
# ---------------------------------------------------------------------------

def build_bonus_report(con: duckdb.DuckDBPyConnection,
                       out_dir: Path,
                       generated_at: str,
                       logger: logging.Logger) -> None:

    # Merchant summary (across all days)
    merch_summary = con.execute(f"""
        SELECT
            merchant_name,
            store_name,
            SUM(total_sales)            AS total_sales,
            SUM(eligible_sales)         AS eligible_sales,
            SUM(total_bonus_earned)     AS total_bonus,
            SUM(total_revenue)          AS total_revenue,
            ROUND(
                100.0 * SUM(eligible_sales) / NULLIF(SUM(total_sales), 0), 2
            )                           AS hit_rate_pct
        FROM {SCHEMA}.merchant_bonus
        GROUP BY merchant_name, store_name
        ORDER BY total_bonus DESC
    """).fetchall()

    # Daily breakdown
    daily = con.execute(f"""
        SELECT
            sale_date,
            merchant_name,
            store_name,
            total_sales,
            eligible_sales,
            total_bonus_earned,
            bonus_hit_rate_pct
        FROM {SCHEMA}.merchant_bonus
        ORDER BY sale_date, merchant_name
    """).fetchall()

    # Per-sale bonus detail (top 50 by bonus)
    detail = con.execute(f"""
        SELECT
            sale_id,
            sale_date,
            merchant_name,
            store_name,
            customer_name,
            product_name,
            total_amount,
            bonus_threshold,
            bonus_amount,
            CASE WHEN is_bonus_eligible THEN 'Yes' ELSE 'No' END AS eligible,
            bonus_earned
        FROM {SCHEMA}.per_sale_bonus
        ORDER BY bonus_earned DESC, total_amount DESC
        LIMIT 50
    """).fetchall()

    def badge_eligible(v: str) -> str:
        cls = "pass" if v == "Yes" else "muted"
        return f'<span class="badge badge-{cls}">{_e(v)}</span>'

    merch_tbl = _table(
        ["Merchant", "Store", "Total Sales", "Eligible Sales",
         "Bonus Earned", "Total Revenue", "Hit Rate"],
        merch_summary,
        [_e, _e, _fmt_int, _fmt_int, _fmt_currency, _fmt_currency, _fmt_pct],
    )

    daily_tbl = _table(
        ["Date", "Merchant", "Store", "Sales", "Eligible",
         "Bonus Earned", "Hit Rate %"],
        daily,
        [_e, _e, _e, _fmt_int, _fmt_int, _fmt_currency, _fmt_pct],
    )

    # custom formatter for detail table (eligible badge)
    def _detail_row(row: tuple) -> str:
        sale_id, sale_date, mname, sname, cname, pname, amt, thr, bamt, elig, earned = row
        elig_badge = (
            '<span class="badge badge-pass">Yes</span>'
            if elig == "Yes" else
            '<span class="badge" style="background:#eee;color:#555">No</span>'
        )
        return (
            f"<tr>"
            f"<td>{_e(sale_id)}</td>"
            f"<td>{_e(sale_date)}</td>"
            f"<td>{_e(mname)}</td>"
            f"<td>{_e(sname)}</td>"
            f"<td>{_e(cname)}</td>"
            f"<td>{_e(pname)}</td>"
            f'<td class="num">{_fmt_currency(amt)}</td>'
            f'<td class="num">{_fmt_currency(thr)}</td>'
            f'<td class="num">{_fmt_currency(bamt)}</td>'
            f"<td>{elig_badge}</td>"
            f'<td class="num">{_fmt_currency(earned)}</td>'
            f"</tr>"
        )

    detail_header = (
        "<table><thead><tr>"
        + "".join(
            f"<th>{h}</th>"
            for h in ["Sale ID", "Date", "Merchant", "Store", "Customer",
                       "Product", "Sale Amount", "Threshold", "Bonus Amt",
                       "Eligible", "Earned"]
        )
        + "</tr></thead><tbody>"
        + "".join(_detail_row(r) for r in detail)
        + "</tbody></table>"
    )

    body = (
        "<h2>Merchant Bonus Summary</h2>" + merch_tbl
        + "<h2>Daily Bonus by Merchant</h2>" + daily_tbl
        + f"<h2>Sale-Level Detail (top {len(detail)} by bonus earned)</h2>"
        + detail_header
    )

    crumb = '<a href="index.html">Home</a> › Bonus Report'
    path  = out_dir / "bonus_report.html"
    path.write_text(
        _page("Bonus Report", crumb, body, generated_at),
        encoding="utf-8",
    )
    logger.info("  Written: %s", path)

# ---------------------------------------------------------------------------
# Report: churn_report.html
# ---------------------------------------------------------------------------

def build_churn_report(con: duckdb.DuckDBPyConnection,
                       out_dir: Path,
                       generated_at: str,
                       logger: logging.Logger) -> None:

    stats = con.execute(f"""
        SELECT
            MAX(reference_date)                                 AS ref_date,
            MAX(inactivity_threshold_days)                      AS threshold,
            COUNT(*)                                            AS total,
            SUM(CASE WHEN is_churned THEN 1 ELSE 0 END)         AS churned,
            SUM(CASE WHEN NOT is_churned THEN 1 ELSE 0 END)     AS active,
            SUM(CASE WHEN is_churned THEN total_spend ELSE 0 END) AS churned_spend
        FROM {SCHEMA}.churn_flags
    """).fetchone()

    ref_date, threshold, total, churned, active, churned_spend = stats

    kpis = _kpi_row(
        (_e(ref_date),         "Reference Date"),
        (f"{threshold} days",  "Inactivity Threshold"),
        (_fmt_int(total),      "Total Customers"),
        (_fmt_int(churned),    "Churned"),
        (_fmt_int(active),     "Active"),
        (_fmt_currency(churned_spend), "Revenue at Risk"),
    )

    churned_rows = con.execute(f"""
        SELECT
            customer_id,
            customer_name,
            customer_email,
            customer_country,
            first_purchase_date,
            last_purchase_date,
            total_transactions,
            total_spend,
            days_since_last_purchase
        FROM {SCHEMA}.churn_flags
        WHERE is_churned
        ORDER BY total_spend DESC
    """).fetchall()

    active_rows = con.execute(f"""
        SELECT
            customer_id,
            customer_name,
            customer_email,
            customer_country,
            first_purchase_date,
            last_purchase_date,
            total_transactions,
            total_spend,
            days_since_last_purchase
        FROM {SCHEMA}.churn_flags
        WHERE NOT is_churned
        ORDER BY last_purchase_date DESC, total_spend DESC
    """).fetchall()

    _cols = ["Customer ID", "Name", "Email", "Country",
             "First Purchase", "Last Purchase",
             "Transactions", "Total Spend", "Days Since Last"]
    _fmts = [_e, _e, _e, _e, _e, _e, _fmt_int, _fmt_currency, _fmt_int]

    def _churn_row(row: tuple, is_churn: bool) -> str:
        badge = (
            '<span class="badge badge-churn">Churned</span>'
            if is_churn else
            '<span class="badge badge-active">Active</span>'
        )
        cells = [f"<td>{badge}</td>"]
        for i, cell in enumerate(row):
            fmt = _fmts[i] if i < len(_fmts) else _e
            cls = ' class="num"' if fmt in (_fmt_currency, _fmt_int) else ""
            cells.append(f"<td{cls}>{fmt(cell)}</td>")
        return "<tr>" + "".join(cells) + "</tr>"

    def _churn_table(rows: list, is_churn: bool) -> str:
        headers = ["Status"] + _cols
        th = "".join(f"<th>{h}</th>" for h in headers)
        body_rows = "".join(_churn_row(r, is_churn) for r in rows)
        return (
            "<table><thead><tr>" + th + "</tr></thead>"
            "<tbody>" + body_rows + "</tbody></table>"
        )

    body = (
        "<h2>Churn Summary</h2>"
        + kpis
        + f"<h2>Churned Customers ({len(churned_rows)})</h2>"
        + (_churn_table(churned_rows, True) if churned_rows
           else "<p>No churned customers.</p>")
        + f"<h2>Active Customers ({len(active_rows)})</h2>"
        + _churn_table(active_rows, False)
    )

    crumb = '<a href="index.html">Home</a> › Customer Churn'
    path  = out_dir / "churn_report.html"
    path.write_text(
        _page("Customer Churn Report", crumb, body, generated_at),
        encoding="utf-8",
    )
    logger.info("  Written: %s", path)

# ---------------------------------------------------------------------------
# Report: merchant_<id>.html  (one per merchant)
# ---------------------------------------------------------------------------

def build_merchant_pages(con: duckdb.DuckDBPyConnection,
                         out_dir: Path,
                         generated_at: str,
                         logger: logging.Logger) -> int:

    merchants = con.execute(f"""
        SELECT DISTINCT merchant_id, MAX(merchant_name) AS merchant_name,
                        MAX(store_name) AS store_name,
                        MAX(merchant_country) AS country
        FROM {SCHEMA}.sales_kpis
        GROUP BY merchant_id
        ORDER BY merchant_id
    """).fetchall()

    count = 0
    for (mid, mname, sname, country) in merchants:

        # Daily KPIs for this merchant
        daily = con.execute(f"""
            SELECT
                sale_date,
                transaction_count,
                units_sold,
                unique_customers,
                total_revenue,
                avg_order_value,
                warn_row_count
            FROM {SCHEMA}.sales_kpis
            WHERE merchant_id = ?
            ORDER BY sale_date
        """, [mid]).fetchall()

        # Product breakdown
        products = con.execute(f"""
            SELECT
                product_id,
                MAX(product_name)   AS product_name,
                COUNT(*)            AS sales,
                SUM(quantity)       AS units,
                SUM(total_amount)   AS revenue,
                SUM(bonus_earned)   AS bonus_earned,
                SUM(CASE WHEN is_bonus_eligible THEN 1 ELSE 0 END) AS eligible
            FROM {SCHEMA}.per_sale_bonus
            WHERE merchant_id = ?
            GROUP BY product_id
            ORDER BY revenue DESC
        """, [mid]).fetchall()

        # Customer list
        customers = con.execute(f"""
            SELECT DISTINCT
                s.customer_id,
                MAX(s.customer_name)    AS customer_name,
                MAX(cf.customer_country) AS country,
                COUNT(*)                AS purchases,
                SUM(s.total_amount)     AS spend,
                MIN(s.sale_date)        AS first,
                MAX(s.sale_date)        AS last_seen,
                cf.is_churned
            FROM {SCHEMA}.per_sale_bonus s
            LEFT JOIN {SCHEMA}.churn_flags cf
                   ON cf.customer_id = s.customer_id
            WHERE s.merchant_id = ?
            GROUP BY s.customer_id, cf.is_churned
            ORDER BY spend DESC
        """, [mid]).fetchall()

        # Bonus summary
        bonus_row = con.execute(f"""
            SELECT
                SUM(total_sales)        AS total_sales,
                SUM(eligible_sales)     AS eligible,
                SUM(total_bonus_earned) AS bonus_earned,
                ROUND(
                    100.0 * SUM(eligible_sales) / NULLIF(SUM(total_sales),0), 2
                )                       AS hit_rate
            FROM {SCHEMA}.merchant_bonus
            WHERE merchant_id = ?
        """, [mid]).fetchone()

        total_rev = sum(float(r[3]) for r in daily if r[3] is not None)

        kpis = _kpi_row(
            (_fmt_currency(total_rev),        "Total Revenue"),
            (_fmt_int(sum(r[1] for r in daily)), "Transactions"),
            (_fmt_int(len(customers)),         "Customers"),
            (_fmt_currency(bonus_row[2] or 0), "Bonus Earned"),
            (_fmt_pct(bonus_row[3]),           "Bonus Hit Rate"),
        )

        daily_tbl = _table(
            ["Sale Date", "Transactions", "Units Sold", "Customers",
             "Revenue", "Avg Order", "Warn Rows"],
            daily,
            [_e, _fmt_int, _fmt_int, _fmt_int,
             _fmt_currency, _fmt_currency, _fmt_int],
        )

        prod_tbl = _table(
            ["Product ID", "Product Name", "Sales", "Units",
             "Revenue", "Bonus Earned", "Eligible Sales"],
            products,
            [_e, _e, _fmt_int, _fmt_int,
             _fmt_currency, _fmt_currency, _fmt_int],
        )

        def _cust_row(row: tuple) -> str:
            cid, cname, ccountry, purch, spend, first, last, churned = row
            badge = (
                '<span class="badge badge-churn">Churned</span>'
                if churned else
                '<span class="badge badge-active">Active</span>'
            )
            return (
                f"<tr><td>{_e(cid)}</td><td>{_e(cname)}</td>"
                f"<td>{_e(ccountry)}</td>"
                f'<td class="num">{_fmt_int(purch)}</td>'
                f'<td class="num">{_fmt_currency(spend)}</td>'
                f"<td>{_e(first)}</td><td>{_e(last)}</td>"
                f"<td>{badge}</td></tr>"
            )

        cust_header = (
            "<table><thead><tr>"
            + "".join(
                f"<th>{h}</th>"
                for h in ["Customer ID", "Name", "Country",
                           "Purchases", "Total Spend",
                           "First Purchase", "Last Purchase", "Status"]
            )
            + "</tr></thead><tbody>"
            + "".join(_cust_row(r) for r in customers)
            + "</tbody></table>"
        )

        body = (
            f"<h2>{_e(mname)} — {_e(sname)}</h2>"
            f"<p style='color:var(--muted);margin:-8px 0 16px'>{_e(country)}</p>"
            + kpis
            + "<h2>Daily Sales</h2>" + daily_tbl
            + "<h2>Product Breakdown</h2>" + prod_tbl
            + "<h2>Customer List</h2>" + cust_header
        )

        crumb = (
            f'<a href="index.html">Home</a> › '
            f'{_e(mname)} — {_e(sname)}'
        )
        fname = f"merchant_{mid}.html"
        path  = out_dir / fname
        path.write_text(
            _page(f"{mname} — {sname}", crumb, body, generated_at),
            encoding="utf-8",
        )
        logger.info("  Written: %s", path)
        count += 1

    return count

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage 5: Render HTML reports from stage_compute tables"
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUT),
        metavar="PATH",
        help=f"Directory to write HTML files into (default: {DEFAULT_OUT})",
    )
    return parser.parse_args()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args   = parse_args()
    logger = setup_logging()
    out_dir = Path(args.output_dir)

    logger.info("=" * 60)
    logger.info("Stage 5 started  (full reprocess)")
    logger.info("Database    %s", DB_PATH)
    logger.info("Output dir  %s", out_dir)
    logger.info("=" * 60)

    if not DB_PATH.exists():
        logger.error("Database not found: %s — run Stages 1–4 first.", DB_PATH)
        sys.exit(1)

    con = duckdb.connect(str(DB_PATH), read_only=True)

    # Verify required tables exist
    required = [
        "sales_kpis", "per_sale_bonus", "merchant_bonus",
        "customer_metrics", "churn_flags",
    ]
    missing = []
    for tbl in required:
        row = con.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = ? AND table_name = ?",
            [SCHEMA, tbl],
        ).fetchone()
        if not row:
            missing.append(f"{SCHEMA}.{tbl}")

    if missing:
        logger.error("Missing tables — run Stage 4 first: %s",
                     ", ".join(missing))
        con.close()
        sys.exit(1)

    # Prepare output directory
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        build_index(con, out_dir, generated_at, logger)
        build_sales_summary(con, out_dir, generated_at, logger)
        build_bonus_report(con, out_dir, generated_at, logger)
        build_churn_report(con, out_dir, generated_at, logger)
        n_merchants = build_merchant_pages(con, out_dir, generated_at, logger)
    except Exception as exc:  # noqa: BLE001
        logger.error("Stage 5 failed: %s", exc, exc_info=True)
        con.close()
        sys.exit(1)

    con.close()

    all_files = sorted(out_dir.glob("*.html"))
    logger.info("")
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("  Output directory : %s", out_dir)
    logger.info("  HTML files written:")
    for f in all_files:
        logger.info("    %s", f.name)
    logger.info("  Merchant pages   : %d", n_merchants)
    logger.info("  Total files      : %d", len(all_files))
    logger.info("=" * 60)
    logger.info("Stage 5 complete.  Open: %s", out_dir / "index.html")


if __name__ == "__main__":
    main()
