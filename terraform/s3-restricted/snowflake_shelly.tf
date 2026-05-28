# ---------------------------------------------------------------------------
# snowflake_shelly.tf — SHELLY_PWR table (data loaded via Snowpipe Streaming)
# ---------------------------------------------------------------------------

# --- File format for single-object JSON files (Shelly) ----------------------
# Separate from JSON_FORMAT (which has strip_outer_array = true for CONTRACTS)

resource "snowflake_file_format" "json_single" {
  name        = "JSON_FORMAT_SINGLE"
  database    = var.snowflake_database
  schema      = var.snowflake_schema
  format_type = "JSON"

  strip_outer_array = false
  null_if           = []
}

# --- Table: schema inferred from sample file --------------------------------

resource "snowflake_execute" "shelly_pwr_table" {
  execute = <<-SQL
    CREATE OR REPLACE TABLE ${var.snowflake_database}.${var.snowflake_schema}.SHELLY_PWR (
      raw_data            VARIANT,
      ingestion_timestamp TIMESTAMP_LTZ,
      source_path         VARCHAR
    )
  SQL
  revert = "DROP TABLE IF EXISTS ${var.snowflake_database}.${var.snowflake_schema}.SHELLY_PWR"
}

# Alias kept so other resources that depend on shelly_pwr_technical_columns continue to resolve.
# The columns are already created above; this is a no-op.
resource "snowflake_execute" "shelly_pwr_technical_columns" {
  execute = "SELECT 1"
  revert  = "SELECT 1"

  depends_on = [snowflake_execute.shelly_pwr_table]
}
