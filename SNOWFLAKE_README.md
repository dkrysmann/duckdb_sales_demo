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
│  File Format: JSON_FORMAT                                       │
│                                                                 │
│  Table: CONTRACTS                                               │
│    └─ Snowpipe: CONTRACTS_PIPE (auto_ingest via SQS)            │
│         └─ Source: S3_STAGE/hubspot/contacts/                   │
│                                                                 │
│  Table: SHELLY_PWR                                              │
│    └─ Snowpipe Streaming (Python producer)                      │
│         └─ Source: s3://piaware/shelly/main_power/status/       │
└─────────────────────────────────────────────────────────────────┘
```

---

## AWS Resources

### IAM Role — `darek-snowflake-access-role`

Assumed by Snowflake's internal IAM user to read from S3. Created by Terraform.

| Attribute        | Value |
|------------------|-------|
| Role ARN         | `arn:aws:iam::000290283155:role/darek-snowflake-access-role` |
| Trusted principal | `arn:aws:iam::606065959540:user/af3m1000-s` (Snowflake's AWS user) |
| Trust condition  | `sts:ExternalId = GPB39838_SFCRole=3_RDifVRYJKyZ0fBiGPOJSydXKQPg=` |

The ExternalId prevents the [confused deputy problem](https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html).
It is obtained from Snowflake by running `DESC INTEGRATION S3_INTEGRATION` and reading `STORAGE_AWS_EXTERNAL_ID`.

### IAM Policy — `darek-snowflake-access-role-s3-policy`

Attached to the role above. Grants read-only access to both S3 buckets.

| Action | Resources |
|--------|-----------|
| `s3:ListBucket`, `s3:GetBucketLocation` | `opp-raw-data-dev`, `opp-sales-data-dev` |
| `s3:GetObject`, `s3:GetObjectVersion`   | `opp-raw-data-dev/*`, `opp-sales-data-dev/*` |

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

### File Format — `JSON_FORMAT`

Used by `CONTRACTS_PIPE` and `INFER_SCHEMA` calls.

```sql
DESC FILE FORMAT PREP.TESTS.JSON_FORMAT;
```

| Setting | Value |
|---------|-------|
| Type | `JSON` |
| `STRIP_OUTER_ARRAY` | `TRUE` — expects JSON arrays `[{...}, ...]` |
| `NULL_IF` | `[]` |

> **Note:** `SHELLY_PWR` uses a separate file format `JSON_FORMAT_SINGLE` (`STRIP_OUTER_ARRAY = FALSE`)
> because Shelly files contain a single JSON object per file, not an array.
> `JSON_FORMAT` (with `STRIP_OUTER_ARRAY = TRUE`) is used only by `CONTRACTS_PIPE`.

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

Schema derived from a Shelly power monitor sample file using `INFER_SCHEMA`.
Two additional technical columns are added after table creation:

| Column | Type | Source |
|--------|------|--------|
| _(all inferred columns)_ | _(typed from JSON)_ | JSON fields via INFER_SCHEMA |
| `ingestion_timestamp` | `TIMESTAMP_LTZ` | `ts_epoch` field converted by the Python producer |
| `source_path` | `VARCHAR` | Full S3 URI of the source file, set by the Python producer |

- **Source sample:** `S3_STAGE/streaming/00001c22-b777-4f42-b136-a7adfcdef066.json`
- **Loaded by:** `shelly_streaming_producer.py` (Snowpipe Streaming API)

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
queue ARN that Snowflake provisions for the pipe. The queue ARN is exposed as a Terraform
output:
```bash
terraform output contracts_pipe_notification_channel
```

---

## Snowpipe Streaming — `SHELLY_PWR` Python Producer

The `shelly_streaming_producer.py` script pushes rows directly into `SHELLY_PWR` via the
Snowpipe Streaming REST API. No S3 staging or SQS notification is involved — rows arrive
in Snowflake within seconds.

```
s3://piaware/shelly/main_power/status/year=YYYY/month=MM/day=DD/
    │
    ├─ boto3 list + read each *.json file
    ├─ convert ts_epoch → ingestion_timestamp (UTC TIMESTAMP_LTZ)
    ├─ set source_path = full S3 URI
    └─ channel.insert_rows() → Snowpipe Streaming API → SHELLY_PWR
```

### Manual Step 1 — Generate an RSA Key Pair

Snowpipe Streaming requires key-pair authentication (password auth is not supported).

```bash
# Generate a 2048-bit RSA private key (PKCS#8, no passphrase)
openssl genrsa 2048 | openssl pkcs8 -topk8 -nocrypt -out ~/.ssh/snowflake_rsa_key.p8

# Extract the public key
openssl rsa -in ~/.ssh/snowflake_rsa_key.p8 -pubout -out ~/.ssh/snowflake_rsa_key.pub

# Protect the private key
chmod 600 ~/.ssh/snowflake_rsa_key.p8
```

### Manual Step 2 — Register the Public Key in Snowflake

Connect to Snowflake as ACCOUNTADMIN and run:

```sql
-- Copy the public key content between the header and footer lines
-- (remove the -----BEGIN PUBLIC KEY----- and -----END PUBLIC KEY----- lines)
ALTER USER dkrysmann SET RSA_PUBLIC_KEY='<paste public key content here>';

-- Verify
DESC USER dkrysmann;
-- RSA_PUBLIC_KEY_FP should now be populated
```

### Manual Step 3 — Grant table privileges to the streaming user

```sql
GRANT INSERT, SELECT ON TABLE PREP.TESTS.SHELLY_PWR TO ROLE ACCOUNTADMIN;
```

### Running the Producer

Install dependencies:
```bash
pip install -r requirements.txt
```

Set environment variables and run:
```bash
export SNOWFLAKE_ACCOUNT="AYWPDSQ-ABB81033"
export SNOWFLAKE_USER="dkrysmann"
export SNOWFLAKE_PRIVATE_KEY_PATH="$HOME/.ssh/snowflake_rsa_key.p8"
export SNOWFLAKE_DATABASE="PREP"
export SNOWFLAKE_SCHEMA="TESTS"
export SNOWFLAKE_TABLE="SHELLY_PWR"
export SNOWFLAKE_ROLE="ACCOUNTADMIN"

# Optional — override source location
export S3_BUCKET="piaware"
export S3_PREFIX="shelly/main_power/status/year=2026/month=05/day=28/"

python shelly_streaming_producer.py
```

### Producer Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SNOWFLAKE_ACCOUNT` | Yes | — | Account locator, e.g. `AYWPDSQ-ABB81033` |
| `SNOWFLAKE_USER` | Yes | — | Snowflake username |
| `SNOWFLAKE_PRIVATE_KEY_PATH` | Yes | — | Path to `.p8` RSA private key |
| `SNOWFLAKE_DATABASE` | No | `PREP` | Target database |
| `SNOWFLAKE_SCHEMA` | No | `TESTS` | Target schema |
| `SNOWFLAKE_TABLE` | No | `SHELLY_PWR` | Target table |
| `SNOWFLAKE_ROLE` | No | `ACCOUNTADMIN` | Snowflake role |
| `S3_BUCKET` | No | `piaware` | Source S3 bucket |
| `S3_PREFIX` | No | `shelly/main_power/status/year=2026/month=05/day=28/` | Source S3 prefix |

---

## Terraform Deployment

### First-time Apply

```bash
cd terraform/s3-restricted

# Set sensitive values as environment variables (do not put in terraform.tfvars)
export TF_VAR_snowflake_external_id="GPB39838_SFCRole=3_RDifVRYJKyZ0fBiGPOJSydXKQPg="

# IMPORTANT: unset SNOWFLAKE_PRIVATE_KEY_PATH if it is set in your shell.
# The Snowflake provider reads it automatically as `private_key_path`, which
# conflicts with the `private_key = file(...)` argument in snowflake.tf.
# That env var is only needed by the Python producer, not by Terraform.
unset SNOWFLAKE_PRIVATE_KEY_PATH

terraform init
terraform plan
terraform apply
```

### Importing Pre-existing Resources

If the storage integration or stage already existed before Terraform managed them:

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
| `TF_VAR_snowflake_password` | Snowflake password for Terraform provider |
| `TF_VAR_snowflake_external_id` | `STORAGE_AWS_EXTERNAL_ID` from `DESC INTEGRATION` |

---

## Verification Queries

```sql
-- Check storage integration
DESC INTEGRATION S3_INTEGRATION;

-- List stages
SHOW STAGES IN SCHEMA PREP.TESTS;

-- Check CONTRACTS table schema
DESC TABLE PREP.TESTS.CONTRACTS;

-- Check SHELLY_PWR table schema (including technical columns)
DESC TABLE PREP.TESTS.SHELLY_PWR;

-- Check Snowpipe status
SELECT SYSTEM$PIPE_STATUS('PREP.TESTS.CONTRACTS_PIPE');

-- Preview recently loaded contracts
SELECT * FROM PREP.TESTS.CONTRACTS LIMIT 10;

-- Preview SHELLY_PWR with technical columns
SELECT source_path, ingestion_timestamp, * FROM PREP.TESTS.SHELLY_PWR
ORDER BY ingestion_timestamp DESC
LIMIT 20;

-- Check Snowpipe Streaming channel lag (run after producer)
SELECT SYSTEM$VERIFY_STORAGE_INTEGRATION('S3_INTEGRATION');
```

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `"private_key_path": conflicts with private_key` | `SNOWFLAKE_PRIVATE_KEY_PATH` env var is set — provider picks it up as `private_key_path`, conflicting with `private_key = file(...)` | Run `unset SNOWFLAKE_PRIVATE_KEY_PATH` before `terraform plan/apply` |
| `AccessDenied: GetPublicAccessBlock` | `aws-user` not in bucket policy allow list | Add to `admin_principals` in `terraform.tfvars` and re-apply |
| `Invalid template: template must be a non-null JSON array` | `INFER_SCHEMA` returned no rows — `STRIP_OUTER_ARRAY` mismatch | Use `(TYPE = JSON STRIP_OUTER_ARRAY = FALSE)` inline if files are single objects |
| `MalformedPolicyDocument: Invalid principal` | IAM user/role referenced in trust policy does not exist | Remove the non-existent principal from `*_trusted_principals` in `terraform.tfvars` |
| `EntityAlreadyExists` on IAM role | Role exists in AWS but not in Terraform state | Run `terraform import aws_iam_role.snowflake <role-name>` |
| Snowpipe Streaming auth failure | RSA key not registered or wrong user | Verify with `DESC USER <user>` — `RSA_PUBLIC_KEY_FP` must be set |
