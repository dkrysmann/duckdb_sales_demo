# ---------------------------------------------------------------------------
# snowflake_streaming_user.tf — Dedicated user + role for Snowpipe Streaming
#
# Principle of least privilege: SHELLY_STREAMER can only INSERT and SELECT on
# SHELLY_PWR. No warehouse, no UI access, no other objects.
# ---------------------------------------------------------------------------

resource "snowflake_account_role" "streaming" {
  name    = "SHELLY_STREAMING_ROLE"
  comment = "Minimal role for the Shelly power monitor Snowpipe Streaming producer"
}

resource "snowflake_user" "streaming" {
  name         = "SHELLY_STREAMER"
  login_name   = "SHELLY_STREAMER"
  display_name = "Shelly Streaming Service Account"
  comment      = "Service account for Snowpipe Streaming — no password, RSA key only"

  default_role = snowflake_account_role.streaming.name

  # Public key content only — no header/footer lines.
  # Extract: openssl rsa -in ~/.ssh/snowflake_rsa_key.p8 -pubout 2>/dev/null \
  #            | grep -v '\-\-\-' | tr -d '\n'
  rsa_public_key = var.snowflake_streaming_rsa_public_key

  must_change_password = false
}

# --- Grants via snowflake_execute (provider-version-agnostic) ---------------

resource "snowflake_execute" "grant_role_to_user" {
  execute = "GRANT ROLE ${snowflake_account_role.streaming.name} TO USER ${snowflake_user.streaming.name}"
  revert  = "REVOKE ROLE ${snowflake_account_role.streaming.name} FROM USER ${snowflake_user.streaming.name}"

  depends_on = [snowflake_account_role.streaming, snowflake_user.streaming]
}

resource "snowflake_execute" "grant_warehouse_usage" {
  execute = "GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE ${snowflake_account_role.streaming.name}"
  revert  = "REVOKE USAGE ON WAREHOUSE COMPUTE_WH FROM ROLE ${snowflake_account_role.streaming.name}"

  depends_on = [snowflake_account_role.streaming]
}

resource "snowflake_execute" "grant_db_usage" {
  execute = "GRANT USAGE ON DATABASE ${var.snowflake_database} TO ROLE ${snowflake_account_role.streaming.name}"
  revert  = "REVOKE USAGE ON DATABASE ${var.snowflake_database} FROM ROLE ${snowflake_account_role.streaming.name}"

  depends_on = [snowflake_account_role.streaming]
}

resource "snowflake_execute" "grant_schema_usage" {
  execute = "GRANT USAGE ON SCHEMA ${var.snowflake_database}.${var.snowflake_schema} TO ROLE ${snowflake_account_role.streaming.name}"
  revert  = "REVOKE USAGE ON SCHEMA ${var.snowflake_database}.${var.snowflake_schema} FROM ROLE ${snowflake_account_role.streaming.name}"

  depends_on = [snowflake_account_role.streaming]
}

resource "snowflake_execute" "grant_table_privileges" {
  execute = "GRANT INSERT, SELECT ON TABLE ${var.snowflake_database}.${var.snowflake_schema}.SHELLY_PWR TO ROLE ${snowflake_account_role.streaming.name}"
  revert  = "REVOKE INSERT, SELECT ON TABLE ${var.snowflake_database}.${var.snowflake_schema}.SHELLY_PWR FROM ROLE ${snowflake_account_role.streaming.name}"

  depends_on = [
    snowflake_account_role.streaming,
    snowflake_execute.shelly_pwr_technical_columns,
  ]
}
