# ---------------------------------------------------------------------------
# snowflake.tf — Snowflake storage integration: IAM role + Snowflake resource
# ---------------------------------------------------------------------------

provider "snowflake" {
  organization_name = var.snowflake_organization_name
  account_name      = var.snowflake_account_name
  user              = var.snowflake_user
  password          = var.snowflake_password
  role              = var.snowflake_role
}

# ===========================================================================
# IAM Role: assumed by Snowflake to read from S3
# ===========================================================================

resource "aws_iam_role" "snowflake" {
  name        = var.snowflake_iam_role_name
  description = "Assumed by Snowflake for external S3 stage access"

  assume_role_policy = data.aws_iam_policy_document.snowflake_trust.json

  tags = {
    Purpose = "snowflake-storage-integration"
    Bucket  = var.snowflake_s3_bucket
  }
}

data "aws_iam_policy_document" "snowflake_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [var.snowflake_aws_iam_user_arn]
    }

    # ExternalId prevents the confused deputy problem
    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [var.snowflake_external_id]
    }
  }
}

# --- S3 permissions --------------------------------------------------------

resource "aws_iam_policy" "snowflake_s3" {
  name        = "${var.snowflake_iam_role_name}-s3-policy"
  description = "S3 read access for Snowflake storage integration on ${var.snowflake_s3_bucket}"

  policy = data.aws_iam_policy_document.snowflake_s3.json
}

data "aws_iam_policy_document" "snowflake_s3" {
  statement {
    sid     = "ListBucket"
    effect  = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [
      "arn:aws:s3:::${var.snowflake_s3_bucket}",
      aws_s3_bucket.this.arn,
    ]
  }

  statement {
    sid     = "ReadObjects"
    effect  = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]
    resources = [
      "arn:aws:s3:::${var.snowflake_s3_bucket}/*",
      "${aws_s3_bucket.this.arn}/*",
    ]
  }
}

resource "aws_iam_role_policy_attachment" "snowflake_s3" {
  role       = aws_iam_role.snowflake.name
  policy_arn = aws_iam_policy.snowflake_s3.arn
}

# ===========================================================================
# Snowflake storage integration
#
# If the integration already exists, import it first:
#   terraform import snowflake_storage_integration.this <INTEGRATION_NAME>
# ===========================================================================

resource "snowflake_storage_integration" "this" {
  name    = var.snowflake_integration_name
  type    = "EXTERNAL_STAGE"
  enabled = true

  storage_provider          = "S3"
  storage_aws_role_arn      = aws_iam_role.snowflake.arn
  storage_allowed_locations = [
    "s3://${var.snowflake_s3_bucket}/",
    "s3://${aws_s3_bucket.this.id}/",
  ]
}

# ===========================================================================
# Snowflake external stage
#
# Import existing stage before applying:
#   terraform import snowflake_stage.s3_stage <DATABASE>|<SCHEMA>|S3_STAGE
# ===========================================================================

resource "snowflake_stage" "s3_stage" {
  name                = "S3_STAGE"
  database            = var.snowflake_database
  schema              = var.snowflake_schema
  url                 = "s3://${var.snowflake_s3_bucket}/"
  storage_integration = snowflake_storage_integration.this.name
}

resource "snowflake_stage" "sales_stage" {
  name                = "S3_SALES_STAGE"
  database            = var.snowflake_database
  schema              = var.snowflake_schema
  url                 = "s3://${aws_s3_bucket.this.id}/"
  storage_integration = snowflake_storage_integration.this.name
}
