#!/usr/bin/env python3
"""
shelly_streaming_producer.py

Reads Shelly power monitor JSON files from S3 and inserts rows into Snowflake
via snowflake-connector-python with RSA key-pair authentication.

Technical columns added per row:
  ingestion_timestamp  TIMESTAMP_LTZ  — ts_epoch from JSON converted to UTC
  source_path          VARCHAR        — full S3 URI of the source file

Configuration (environment variables):
  SNOWFLAKE_ACCOUNT          account locator, e.g. AYWPDSQ-ABB81033
  SNOWFLAKE_USER             Snowflake user
  SNOWFLAKE_PRIVATE_KEY_PATH path to PEM-encoded RSA private key (no passphrase)
  SNOWFLAKE_DATABASE         target database  (default: PREP)
  SNOWFLAKE_SCHEMA           target schema    (default: TESTS)
  SNOWFLAKE_TABLE            target table     (default: SHELLY_PWR)
  SNOWFLAKE_ROLE             Snowflake role   (default: SHELLY_STREAMING_ROLE)
  S3_BUCKET                  source bucket    (default: piaware)
  S3_PREFIX                  source prefix    (default: shelly/main_power/...)

Usage:
  export SNOWFLAKE_ACCOUNT=AYWPDSQ-ABB81033
  export SNOWFLAKE_USER=SHELLY_STREAMER
  export SNOWFLAKE_PRIVATE_KEY_PATH=~/.ssh/snowflake_rsa_key.p8
  python shelly_streaming_producer.py
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3
import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"ERROR: required environment variable {name!r} is not set.\n"
                         f"  export {name}=<value>")
    return val

SNOWFLAKE_ACCOUNT  = _require("SNOWFLAKE_ACCOUNT")
SNOWFLAKE_USER     = _require("SNOWFLAKE_USER")
SNOWFLAKE_KEY_PATH = _require("SNOWFLAKE_PRIVATE_KEY_PATH")
SNOWFLAKE_DATABASE = os.environ.get("SNOWFLAKE_DATABASE", "PREP")
SNOWFLAKE_SCHEMA   = os.environ.get("SNOWFLAKE_SCHEMA",   "TESTS")
SNOWFLAKE_TABLE    = os.environ.get("SNOWFLAKE_TABLE",    "SHELLY_PWR")
SNOWFLAKE_ROLE      = os.environ.get("SNOWFLAKE_ROLE",      "SHELLY_STREAMING_ROLE")
SNOWFLAKE_WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")

S3_BUCKET = os.environ.get("S3_BUCKET", "piaware")
S3_PREFIX = os.environ.get(
    "S3_PREFIX",
    "shelly/main_power/status/year=2026/month=05/day=28/",
)

BATCH_SIZE = 10

# ── RSA key-pair auth ─────────────────────────────────────────────────────────

def _load_private_key(path: str) -> bytes:
    with open(os.path.expanduser(path), "rb") as fh:
        key = serialization.load_pem_private_key(
            fh.read(),
            password=None,
            backend=default_backend(),
        )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

# ── S3 helpers ────────────────────────────────────────────────────────────────

def _list_json_files(bucket: str, prefix: str):
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json"):
                yield obj["Key"]


def _read_json_file(bucket: str, key: str):
    """Yields one dict per record. Handles both single objects and NDJSON."""
    s3 = boto3.client("s3")
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    for line in body.splitlines():
        line = line.strip()
        if line:
            yield json.loads(line)

# ── Row enrichment ────────────────────────────────────────────────────────────

def _enrich(row: dict, s3_key: str) -> tuple:
    ts_epoch = row.get("ts_epoch")
    ingestion_ts = (
        datetime.fromtimestamp(int(ts_epoch) / 1000, tz=timezone.utc).isoformat()
        if ts_epoch is not None else None
    )
    return (json.dumps(row), ingestion_ts, f"s3://{S3_BUCKET}/{s3_key}")

# ── Flush helper ──────────────────────────────────────────────────────────────

_INSERT_SQL = (
    f"INSERT INTO {SNOWFLAKE_DATABASE}.{SNOWFLAKE_SCHEMA}.{SNOWFLAKE_TABLE} "
    "(raw_data, ingestion_timestamp, source_path) "
    "SELECT PARSE_JSON(%s), %s::TIMESTAMP_LTZ, %s"
)

def _flush(cursor, batch: list[tuple], total: int) -> int:
    for params in batch:
        cursor.execute(_INSERT_SQL, params)
    total += len(batch)
    log.info("Flushed %d rows  |  cumulative: %d", len(batch), total)
    return total

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    conn = snowflake.connector.connect(
        account=SNOWFLAKE_ACCOUNT,
        user=SNOWFLAKE_USER,
        private_key=_load_private_key(SNOWFLAKE_KEY_PATH),
        database=SNOWFLAKE_DATABASE,
        schema=SNOWFLAKE_SCHEMA,
        role=SNOWFLAKE_ROLE,
        warehouse=SNOWFLAKE_WAREHOUSE,
    )

    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE WAREHOUSE {SNOWFLAKE_WAREHOUSE}")
            files = list(_list_json_files(S3_BUCKET, S3_PREFIX))
            log.info("Found %d file(s) under s3://%s/%s", len(files), S3_BUCKET, S3_PREFIX)

            batch: list[tuple] = []
            total = 0

            for s3_key in files:
                log.info("Reading  s3://%s/%s", S3_BUCKET, s3_key)
                for row in _read_json_file(S3_BUCKET, s3_key):
                    batch.append(_enrich(row, s3_key))
                    if len(batch) >= BATCH_SIZE:
                        total = _flush(cursor, batch, total)
                        batch = []

            if batch:
                total = _flush(cursor, batch, total)

        conn.commit()
        log.info("Complete. Total rows inserted: %d", total)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
