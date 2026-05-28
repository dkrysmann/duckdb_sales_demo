# ---------------------------------------------------------------------------
# snowflake_contracts.tf — CONTRACTS table + Snowpipe from opp-raw-data-dev
# ---------------------------------------------------------------------------

# --- JSON file format -------------------------------------------------------

resource "snowflake_file_format" "json" {
  name        = "JSON_FORMAT"
  database    = var.snowflake_database
  schema      = var.snowflake_schema
  format_type = "JSON"

  strip_outer_array = true
  null_if           = []
}

# --- Table: schema inferred from sample file --------------------------------
#
# Uses INFER_SCHEMA so column names and types are derived from the JSON file.
# Taint this resource to re-infer if the source schema changes:
#   terraform taint snowflake_execute.contracts_table && terraform apply

resource "snowflake_execute" "contracts_table" {
  execute = <<-SQL
    CREATE OR REPLACE TABLE ${var.snowflake_database}.${var.snowflake_schema}.CONTRACTS
    USING TEMPLATE (
      SELECT ARRAY_AGG(OBJECT_CONSTRUCT(*))
      FROM TABLE(
        INFER_SCHEMA(
          LOCATION    => '@${var.snowflake_database}.${var.snowflake_schema}.S3_STAGE/hubspot/contacts/2026-05-20/contacts_102909.json',
          FILE_FORMAT => '${var.snowflake_database}.${var.snowflake_schema}.JSON_FORMAT'
        )
      )
    )
  SQL
  revert = "DROP TABLE IF EXISTS ${var.snowflake_database}.${var.snowflake_schema}.CONTRACTS"

  depends_on = [
    snowflake_file_format.json,
    snowflake_stage.s3_stage,
  ]
}

# --- Snowpipe ---------------------------------------------------------------

resource "snowflake_pipe" "contracts" {
  name        = "CONTRACTS_PIPE"
  database    = var.snowflake_database
  schema      = var.snowflake_schema
  auto_ingest = true

  copy_statement = <<-SQL
    COPY INTO ${var.snowflake_database}.${var.snowflake_schema}.CONTRACTS
    FROM @${var.snowflake_database}.${var.snowflake_schema}.S3_STAGE/hubspot/contacts/
    FILE_FORMAT = (FORMAT_NAME = '${var.snowflake_database}.${var.snowflake_schema}.JSON_FORMAT')
    MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE
  SQL

  depends_on = [snowflake_execute.contracts_table]
}

# --- S3 event notification → Snowpipe SQS ----------------------------------
#
# NOTE: aws_s3_bucket_notification replaces ALL existing notifications on the
# bucket. If other notifications exist on opp-raw-data-dev, add them here too
# as additional queue {} blocks to avoid losing them.

resource "aws_s3_bucket_notification" "contracts_pipe" {
  bucket = var.snowflake_s3_bucket

  queue {
    queue_arn     = snowflake_pipe.contracts.notification_channel
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "hubspot/contacts/"
    filter_suffix = ".json"
  }

}
