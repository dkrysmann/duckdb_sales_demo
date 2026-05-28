# ---------------------------------------------------------------------------
# variables.tf — input parameters for the restricted S3 bucket
# ---------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region where the bucket will be created"
  type        = string
  default     = "eu-central-1"
}

variable "bucket_name" {
  description = "Globally unique S3 bucket name (lowercase, no underscores)"
  type        = string
}

variable "environment" {
  description = "Environment label (e.g. dev, tst, acc, prd) — added as a tag"
  type        = string
  default     = "prd"
}

variable "project" {
  description = "Project name — added as a tag"
  type        = string
  default     = "sales-pipeline"
}

# ---------------------------------------------------------------------------
# IAM roles — trusted principals (who may assume the role)
# ---------------------------------------------------------------------------

variable "reader_trusted_principals" {
  description = <<-EOT
    List of ARNs allowed to assume the read role.
    Example: ["arn:aws:iam::123456789012:root", "arn:aws:iam::123456789012:user/alice"]
  EOT
  type        = list(string)
}

variable "writer_trusted_principals" {
  description = <<-EOT
    List of ARNs allowed to assume the write role.
    Example: ["arn:aws:iam::123456789012:root", "arn:aws:iam::123456789012:user/deploy-bot"]
  EOT
  type        = list(string)
}

variable "reader_role_name" {
  description = "Name of the IAM role with read access"
  type        = string
  default     = "s3-reader"
}

variable "writer_role_name" {
  description = "Name of the IAM role with write access"
  type        = string
  default     = "s3-writer"
}

# ---------------------------------------------------------------------------
# Bucket options
# ---------------------------------------------------------------------------

variable "versioning_enabled" {
  description = "Enable S3 object versioning (recommended for audit trails)"
  type        = bool
  default     = true
}

variable "force_destroy" {
  description = "Automatically delete all objects on 'terraform destroy'. Set to FALSE in production."
  type        = bool
  default     = false
}

variable "log_bucket" {
  description = "Name of the S3 bucket for access logs. Leave empty to disable logging."
  type        = string
  default     = ""
}

variable "admin_principals" {
  description = <<-EOT
    List of IAM principal ARNs granted full S3 access (s3:*) to the bucket.
    These principals are also exempt from the bucket-wide deny policy, so both
    the explicit Allow and the deny exemption are applied together.
    Example: ["arn:aws:iam::123456789012:user/aws-user"]
  EOT
  type        = list(string)
  default     = []
}

# ---------------------------------------------------------------------------
# Snowflake storage integration
# ---------------------------------------------------------------------------

variable "snowflake_organization_name" {
  description = "Snowflake organization name (the part before the dash in ORGNAME-ACCOUNTNAME)"
  type        = string
}

variable "snowflake_account_name" {
  description = "Snowflake account name (the part after the dash in ORGNAME-ACCOUNTNAME)"
  type        = string
}

variable "snowflake_user" {
  description = "Snowflake user for Terraform"
  type        = string
}

variable "snowflake_private_key_path" {
  description = "Path to the RSA private key .p8 file used by the Snowflake Terraform provider"
  type        = string
}

variable "snowflake_role" {
  description = "Snowflake role used by Terraform (needs CREATE INTEGRATION privilege)"
  type        = string
  default     = "ACCOUNTADMIN"
}

variable "snowflake_integration_name" {
  description = "Name of the Snowflake storage integration resource"
  type        = string
}

variable "snowflake_iam_role_name" {
  description = "Name of the AWS IAM role that Snowflake will assume"
  type        = string
  default     = "darek-snowflake-access-role"
}

variable "snowflake_aws_iam_user_arn" {
  description = "STORAGE_AWS_IAM_USER_ARN from DESC INTEGRATION — the Snowflake IAM user that assumes the role"
  type        = string
}

variable "snowflake_external_id" {
  description = "STORAGE_AWS_EXTERNAL_ID from DESC INTEGRATION — used in the role trust condition"
  type        = string
  sensitive   = true
}

variable "snowflake_s3_bucket" {
  description = "S3 bucket name Snowflake is allowed to read (STORAGE_ALLOWED_LOCATIONS)"
  type        = string
}

variable "snowflake_database" {
  description = "Snowflake database where the external stage lives"
  type        = string
}

variable "snowflake_schema" {
  description = "Snowflake schema where the external stage lives"
  type        = string
}

variable "snowflake_streaming_rsa_public_key" {
  description = <<-EOT
    RSA public key for the SHELLY_STREAMER service account (content only, no header/footer).
    Extract from your private key:
      openssl rsa -in ~/.ssh/snowflake_rsa_key.p8 -pubout 2>/dev/null | grep -v '\-\-\-' | tr -d '\n'
  EOT
  type        = string
}
