# Snowflake Integration

This document describes all Snowflake and supporting AWS resources managed by the
Terraform configuration in `terraform/s3-restricted/` and the Python producer in
`shelly_streaming_producer.py`.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  AWS account 000290283155                                       │
│                                                                 │
│  IAM Role: darek-snowflake-access-role ◄── Snowflake assumes   │
│    └─ Policy: s3:ListBucket, s3:GetObject                       │
│         ├─ opp-raw-data-dev   (all prefixes)                    │
│         └─ opp-sales-data-dev (all prefixes)                    │
└───────────────────────┬─────────────────────────────────────────┘
                        │ STS AssumeRole (ExternalId)
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│  Snowflake account  AYWPDSQ-ABB81033                            │
│  Database: PREP   Schema: TESTS                                 │
│                                                                 │
│  Storage Integration: S3_INTEGRATION                            │
│    ├─ Stage: S3_STAGE       → s3://opp-raw-data-dev/            │
│    └─ Stage: S3_SALES_STAGE → s3://opp-sales-data-dev/         │
│                                                                 │
│  File Formats                                                   │
│    ├─ JSON_FORMAT        (STRIP_OUTER_ARRAY=TRUE)  ← CONTRACTS  │
│    └─ JSON_FORMAT_SINGLE (STRIP_OUTER_ARRAY=FALSE) ← unused     │
│                                                                 │
│  Table: CONTRACTS                                               │
│    └─ Snowpipe: CONTRACTS_PIPE (auto_ingest via SQS)            │
│         └─ Source: S3_STAGE/hubspot/contacts/                   │
│                                                                 │
│  Table: SHELLY_PWR (raw_data VARIANT, ingestion_timestamp,      │
│                     source_path)                                │
│    └─ Python producer (shelly_streaming_producer.py)            │
│         └─ Source: s3://piaware/shelly/main_power/status/       │
│                                                                 │
│  Service account: SHELLY_STREAMER                               │
│    └─ Role: SHELLY_STREAMING_ROLE                               │
│         └─ USAGE: PREP, TESTS, COMPUTE_WH                       │
│         └─ INSERT, SELECT: SHELLY_PWR                           │
└─────────────────────────────────────────────────────────────────┘
```

---

## AWS Resources

### IAM Role — `darek-snowflake-access-role`

Assumed by Snowflake's internal IAM user to read from S3. Created by Terraform.

| Attribute | Value |
|-----------|-------|
| Role ARN | `arn:aws:iam::000290283155:role/darek-snowflake-access-role` |
| Trusted principal | `arn:aws:iam::606065959540:user/af3m1000-s` (Snowflake's AWS user) |
| Trust condition | `sts:ExternalId = GPB39838_SFCRole=3_RDifVRYJKyZ0fBiGPOJSydXKQPg=` |

The ExternalId prevents the [confused deputy problem](https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html).
Obtained by running `DESC INTEGRATION S3_INTEGRATION` and reading `STORAGE_AWS_EXTERNAL_ID`.

### IAM Policy — `darek-snowflake-access-role-s3-policy`

Attached to the role above. Grants read-only access to both S3 buckets.

| Action | Resources |
|--------|-----------|
| `s3:ListBucket`, `s3:GetBucketLocation` | `opp-raw-data-dev`, `opp-sales-data-dev` |
| `s3:GetObject`, `s3:GetObjectVersion` | `opp-raw-data-dev/*`, `opp-sales-data-dev/*` |

---

## Snowflake Resources

All resources live in database **PREP**, schema **TESTS** unless stated otherwise.

### Storage Integration — `S3_INTEGRATION`

Connects Snowflake to AWS via the IAM role. Managed by Terraform.

```sql
DESC INTEGRATION S3_INTEGRATION;
```

| Property | Value |
|----------|-------|
| Type | `EXTERNAL_STAGE` |
| Storage provider | `S3` |
| AWS role ARN | `arn:aws:iam::000290283155:role/darek-snowflake-access-role` |
| Allowed locations | `s3://opp-raw-data-dev/`, `s3://opp-sales-data-dev/` |

> **If the integration already exists** before the first `terraform apply`, import it:
> ```bash
> terraform import snowflake_storage_integration.this S3_INTEGRATION
> ```

### External Stages

| Terraform resource | Stage name | URL |
|--------------------|------------|-----|
| `snowflake_stage.s3_stage` | `PREP.TESTS.S3_STAGE` | `s3://opp-raw-data-dev/` |
| `snowflake_stage.sales_stage` | `PREP.TESTS.S3_SALES_STAGE` | `s3://opp-sales-data-dev/` |

Both stages use `S3_INTEGRATION` for authentication — no credentials stored in Snowflake.

> **If `S3_STAGE` already exists** before the first `terraform apply`, import it:
> ```bash
> terraform import snowflake_stage.s3_stage PREP|TESTS|S3_STAGE
> ```

### File Formats

| Name | `STRIP_OUTER_ARRAY` | Used by |
|------|---------------------|---------|
| `JSON_FORMAT` | `TRUE` — expects arrays `[{...}]` | `CONTRACTS_PIPE` |
| `JSON_FORMAT_SINGLE` | `FALSE` — single JSON object per file | Reserved (not currently used) |

---

## Tables

### `PREP.TESTS.CONTRACTS`

Schema derived at deploy time from a HubSpot contacts sample file using `INFER_SCHEMA`.
Columns are typed automatically based on the JSON structure.

- **Source sample:** `S3_STAGE/hubspot/contacts/2026-05-20/contacts_102909.json`
- **Loaded by:** `CONTRACTS_PIPE` (Snowpipe, S3 auto-ingest)

To re-infer the schema after a source schema change:
```bash
terraform taint snowflake_execute.contracts_table
terraform apply
```

### `PREP.TESTS.SHELLY_PWR`

Fixed schema — raw JSON stored as VARIANT alongside two typed technical columns.
No `INFER_SCHEMA` is used; the schema is declared explicitly in Terraform.

| Column | Type | Source |
|--------|------|--------|
| `raw_data` | `VARIANT` | Full JSON object from the Shelly device, serialised by the Python producer |
| `ingestion_timestamp` | `TIMESTAMP_LTZ` | `ts_epoch` (milliseconds) converted to UTC by the Python producer |
| `source_path` | `VARCHAR` | Full S3 URI of the source file, set by the Python producer |

- **Loaded by:** `shelly_streaming_producer.py`

Query sensor fields using Snowflake VARIANT dot-notation:
```sql
SELECT raw_data:apower::FLOAT   AS active_power_w,
       raw_data:voltage::FLOAT  AS voltage_v,
       raw_data:current::FLOAT  AS current_a,
       ingestion_timestamp,
       source_path
FROM PREP.TESTS.SHELLY_PWR
ORDER BY ingestion_timestamp DESC
LIMIT 20;
```

---

## Snowpipe — `PREP.TESTS.CONTRACTS_PIPE`

File-based auto-ingest pipe. Triggered automatically when new files land in S3.

| Setting | Value |
|---------|-------|
| Source stage | `PREP.TESTS.S3_STAGE/hubspot/contacts/` |
| File format | `PREP.TESTS.JSON_FORMAT` |
| Column mapping | `MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE` |
| Auto-ingest | `TRUE` — driven by S3 event notification → SQS |

The S3 event notification on `opp-raw-data-dev` (prefix `hubspot/contacts/`, suffix `.json`)
is created by Terraform (`aws_s3_bucket_notification.contracts_pipe`) and wired to the SQS
queue ARN that Snowflake provisions for the pipe:

```bash
terraform output contracts_pipe_notification_channel
```

---

## Snowflake Service Account — `SHELLY_STREAMER`

A dedicated least-privilege account for the Python producer. No password — RSA key-pair only.

| Resource | Name |
|----------|------|
| User | `SHELLY_STREAMER` |
| Role | `SHELLY_STREAMING_ROLE` |
| Warehouse | `COMPUTE_WH` |
| Privileges | `USAGE` on `PREP`, `TESTS`, `COMPUTE_WH`; `INSERT`, `SELECT` on `SHELLY_PWR` |

All resources are managed by Terraform in `snowflake_streaming_user.tf`.

---

## Python Producer — `shelly_streaming_producer.py`

Reads Shelly power monitor JSON from S3 and inserts rows into `SHELLY_PWR` using
`snowflake-connector-python` with RSA key-pair authentication. Runs as a batch job.

```
s3://piaware/shelly/main_power/status/year=YYYY/month=MM/day=DD/
    │
    ├─ boto3 list + read each *.json file (single-object or NDJSON per file)
    ├─ wrap full JSON as raw_data (VARIANT)
    ├─ convert ts_epoch (milliseconds) → ingestion_timestamp (UTC TIMESTAMP_LTZ)
    ├─ set source_path = full S3 URI
    └─ cursor.execute(INSERT ... SELECT PARSE_JSON(%s), ...) × batch_size
```

### Manual Step 1 — Generate an RSA Key Pair

Snowflake key-pair authentication requires an RSA private key (no passphrase).

```bash
# Generate a 2048-bit RSA private key (PKCS#8, no passphrase)
openssl genrsa 2048 | openssl pkcs8 -topk8 -nocrypt -out ~/.ssh/snowflake_rsa_key.p8

# Extract the public key
openssl rsa -in ~/.ssh/snowflake_rsa_key.p8 -pubout -out ~/.ssh/snowflake_rsa_key.pub

# Protect the private key
chmod 600 ~/.ssh/snowflake_rsa_key.p8
```

### Manual Step 2 — Register the Public Key in Snowflake

Connect to Snowflake as ACCOUNTADMIN and run for **both** accounts that use the key:

```sql
-- For your admin/Terraform user
ALTER USER dkrysmann SET RSA_PUBLIC_KEY='<paste public key content — no header/footer lines>';

-- For the dedicated streaming service account
ALTER USER SHELLY_STREAMER SET RSA_PUBLIC_KEY='<same or different public key>';

-- Verify — RSA_PUBLIC_KEY_FP should be populated
DESC USER dkrysmann;
DESC USER SHELLY_STREAMER;
```

To extract the public key content (no PEM header/footer):
```bash
openssl rsa -in ~/.ssh/snowflake_rsa_key.p8 -pubout 2>/dev/null | grep -v '\-\-\-' | tr -d '\n'
```

### Manual Step 3 — Grant Warehouse Access

Until `terraform apply` has been run with the latest `snowflake_streaming_user.tf`,
grant warehouse access manually:

```sql
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE SHELLY_STREAMING_ROLE;
```

### Running the Producer

Install dependencies:
```bash
pip install -r requirements.txt
```

Set environment variables and run:
```bash
export SNOWFLAKE_ACCOUNT="AYWPDSQ-ABB81033"
export SNOWFLAKE_USER="SHELLY_STREAMER"
export SNOWFLAKE_PRIVATE_KEY_PATH="$HOME/.ssh/snowflake_rsa_key.p8"

# Optional overrides (defaults shown)
export SNOWFLAKE_DATABASE="PREP"
export SNOWFLAKE_SCHEMA="TESTS"
export SNOWFLAKE_TABLE="SHELLY_PWR"
export SNOWFLAKE_ROLE="SHELLY_STREAMING_ROLE"
export SNOWFLAKE_WAREHOUSE="COMPUTE_WH"
export S3_BUCKET="piaware"
export S3_PREFIX="shelly/main_power/status/year=2026/month=05/day=28/"

python shelly_streaming_producer.py
```

### Producer Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SNOWFLAKE_ACCOUNT` | Yes | — | Account locator, e.g. `AYWPDSQ-ABB81033` |
| `SNOWFLAKE_USER` | Yes | — | Snowflake username |
| `SNOWFLAKE_PRIVATE_KEY_PATH` | Yes | — | Path to `.p8` RSA private key (no passphrase) |
| `SNOWFLAKE_DATABASE` | No | `PREP` | Target database |
| `SNOWFLAKE_SCHEMA` | No | `TESTS` | Target schema |
| `SNOWFLAKE_TABLE` | No | `SHELLY_PWR` | Target table |
| `SNOWFLAKE_ROLE` | No | `SHELLY_STREAMING_ROLE` | Snowflake role |
| `SNOWFLAKE_WAREHOUSE` | No | `COMPUTE_WH` | Warehouse for query execution |
| `S3_BUCKET` | No | `piaware` | Source S3 bucket |
| `S3_PREFIX` | No | `shelly/main_power/status/year=2026/month=05/day=28/` | Source S3 prefix |

> **Important:** Do not set `SNOWFLAKE_PRIVATE_KEY_PATH` in the shell when running
> `terraform plan` / `terraform apply`. The Snowflake Terraform provider also reads that
> env var and it conflicts with the `private_key = file(...)` argument in the provider block.
> ```bash
> unset SNOWFLAKE_PRIVATE_KEY_PATH   # before terraform commands
> ```

---

## Terraform Deployment

### First-time Apply

```bash
cd terraform/s3-restricted

# Required sensitive variable — do not put in terraform.tfvars
export TF_VAR_snowflake_external_id="GPB39838_SFCRole=3_RDifVRYJKyZ0fBiGPOJSydXKQPg="

# IMPORTANT: unset this before running Terraform (conflicts with private_key in provider)
unset SNOWFLAKE_PRIVATE_KEY_PATH

terraform init
terraform plan
terraform apply
```

### Importing Pre-existing Resources

If the storage integration, stage, or IAM role already existed before Terraform managed them:

```bash
terraform import snowflake_storage_integration.this S3_INTEGRATION
terraform import snowflake_stage.s3_stage PREP|TESTS|S3_STAGE
terraform import aws_iam_role.snowflake darek-snowflake-access-role
```

### Key Variables (`terraform.tfvars`)

| Variable | Value |
|----------|-------|
| `snowflake_organization_name` | `AYWPDSQ` |
| `snowflake_account_name` | `ABB81033` |
| `snowflake_user` | `dkrysmann` |
| `snowflake_private_key_path` | `~/.ssh/snowflake_rsa_key.p8` |
| `snowflake_role` | `ACCOUNTADMIN` |
| `snowflake_integration_name` | `S3_INTEGRATION` |
| `snowflake_database` | `PREP` |
| `snowflake_schema` | `TESTS` |
| `snowflake_iam_role_name` | `darek-snowflake-access-role` |
| `snowflake_aws_iam_user_arn` | `arn:aws:iam::606065959540:user/af3m1000-s` |
| `snowflake_s3_bucket` | `opp-raw-data-dev` |

Sensitive variables — set via env vars, never commit to git:

| Env variable | Description |
|--------------|-------------|
| `TF_VAR_snowflake_external_id` | `STORAGE_AWS_EXTERNAL_ID` from `DESC INTEGRATION S3_INTEGRATION` |

---

## Verification Queries

```sql
-- Check storage integration
DESC INTEGRATION S3_INTEGRATION;

-- List stages
SHOW STAGES IN SCHEMA PREP.TESTS;

-- Check CONTRACTS table schema
DESC TABLE PREP.TESTS.CONTRACTS;

-- Check SHELLY_PWR table schema
DESC TABLE PREP.TESTS.SHELLY_PWR;

-- Check Snowpipe status
SELECT SYSTEM$PIPE_STATUS('PREP.TESTS.CONTRACTS_PIPE');

-- Preview recently loaded contracts
SELECT * FROM PREP.TESTS.CONTRACTS LIMIT 10;

-- Preview SHELLY_PWR — extract typed fields from VARIANT
SELECT raw_data:apower::FLOAT  AS active_power_w,
       raw_data:voltage::FLOAT AS voltage_v,
       raw_data:freq::FLOAT    AS freq_hz,
       ingestion_timestamp,
       source_path
FROM PREP.TESTS.SHELLY_PWR
ORDER BY ingestion_timestamp DESC
LIMIT 20;

-- Count rows by source file
SELECT source_path, COUNT(*) AS rows
FROM PREP.TESTS.SHELLY_PWR
GROUP BY source_path
ORDER BY rows DESC;

-- Check streaming service account
DESC USER SHELLY_STREAMER;
SHOW GRANTS TO ROLE SHELLY_STREAMING_ROLE;
```

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `"private_key_path": conflicts with private_key` | `SNOWFLAKE_PRIVATE_KEY_PATH` env var is set — the Terraform provider also reads it, conflicting with `private_key = file(...)` | `unset SNOWFLAKE_PRIVATE_KEY_PATH` before `terraform plan/apply` |
| `260002: password is empty` | Provider defaulting to password auth instead of RSA | Ensure `authenticator = "JWT"` is set in `provider "snowflake"` block in `snowflake.tf` |
| `No active warehouse selected` | `SHELLY_STREAMING_ROLE` lacks `USAGE ON WAREHOUSE COMPUTE_WH` | Run `GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE SHELLY_STREAMING_ROLE;` then `terraform apply` |
| `AccessDenied: GetPublicAccessBlock` | `aws-user` not in bucket policy allow list | Add to `admin_principals` in `terraform.tfvars` and re-apply |
| `Invalid template: template must be a non-null JSON array` | `INFER_SCHEMA` returned no rows — sample file missing or wrong stage | `SHELLY_PWR` now uses a fixed DDL — this error only applies to `CONTRACTS`; check that the HubSpot sample file exists at the stage path |
| `MalformedPolicyDocument: Invalid principal` | IAM user/role in trust policy does not exist | Remove the non-existent principal from `*_trusted_principals` in `terraform.tfvars` |
| `EntityAlreadyExists` on IAM role | Role exists in AWS but not in Terraform state | `terraform import aws_iam_role.snowflake <role-name>` |
| `252001: Failed to rewrite multi-row insert` | `executemany` with `PARSE_JSON()` can't be rewritten as multi-row | Producer uses a `for` loop over `cursor.execute()` — `executemany` is not used |
| `KeyError: 'SNOWFLAKE_ACCOUNT'` | Required env vars not exported | `export SNOWFLAKE_ACCOUNT=AYWPDSQ-ABB81033` etc. before running the producer |
