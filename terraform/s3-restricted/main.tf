# ---------------------------------------------------------------------------
# main.tf — S3 bucket restricted to two IAM roles
#
# Architecture:
#   • S3 bucket with AES-256 encryption and public access block
#   • Bucket policy: DENY everything for everyone except the read role,
#     write role, and the AWS account root (to prevent lock-out)
#   • Read role:  GetObject, ListBucket, GetBucketLocation
#   • Write role: PutObject, DeleteObject, ListBucket, GetObject, GetBucketLocation
#   • No shared passwords or access keys — IAM roles only
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    snowflake = {
      source  = "Snowflake-Labs/snowflake"
      version = "~> 0.94"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# Retrieve the current AWS account ID (needed for ARN construction)
data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  reader_arn   = "arn:aws:iam::${local.account_id}:role/${var.reader_role_name}"
  writer_arn   = "arn:aws:iam::${local.account_id}:role/${var.writer_role_name}"
  account_root = "arn:aws:iam::${local.account_id}:root"
  bucket_arn   = "arn:aws:s3:::${var.bucket_name}"

  # Combined list of principals exempt from the bucket-wide deny
  allowed_principals = concat(
    [local.reader_arn, local.writer_arn, local.account_root],
    var.admin_principals,
  )
}

# ===========================================================================
# S3 Bucket
# ===========================================================================

resource "aws_s3_bucket" "this" {
  bucket        = var.bucket_name
  force_destroy = var.force_destroy

  tags = {
    Name = var.bucket_name
  }
}

# --- Versioning ------------------------------------------------------------
resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id

  versioning_configuration {
    status = var.versioning_enabled ? "Enabled" : "Suspended"
  }
}

# --- Encryption (server-side, AES-256) -------------------------------------
resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# --- Block ALL public access -----------------------------------------------
resource "aws_s3_bucket_public_access_block" "this" {
  bucket = aws_s3_bucket.this.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- Access logging (optional) ---------------------------------------------
resource "aws_s3_bucket_logging" "this" {
  count = var.log_bucket != "" ? 1 : 0

  bucket        = aws_s3_bucket.this.id
  target_bucket = var.log_bucket
  target_prefix = "${var.bucket_name}/"
}

# ===========================================================================
# Bucket policy — deny everyone except the read role, write role and account root
#
# Why the deny approach?
#   ArnNotLike + Effect=Deny causes AWS to deny EVERY request whose
#   principal ARN does not match one of the three exceptions.
#   This is more robust than a positive Allow-only policy because it also
#   blocks actions that could arrive via other mechanisms (e.g. a resource-based
#   Allow via an organisation policy).
# ===========================================================================

resource "aws_s3_bucket_policy" "this" {
  bucket = aws_s3_bucket.this.id

  # Wait until the public access block is in place
  depends_on = [aws_s3_bucket_public_access_block.this]

  policy = data.aws_iam_policy_document.bucket_policy.json
}

data "aws_iam_policy_document" "bucket_policy" {

  # ── Statement 1: require HTTPS ──────────────────────────────────────────
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      local.bucket_arn,
      "${local.bucket_arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # ── Statement 2: deny everyone EXCEPT the two roles + account root ──────
  statement {
    sid     = "DenyAllExceptAuthorisedRoles"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      local.bucket_arn,
      "${local.bucket_arn}/*",
    ]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    condition {
      test     = "ArnNotLike"
      variable = "aws:PrincipalArn"
      values   = local.allowed_principals
    }
  }

  # ── Statement 3: full S3 access for admin principals ────────────────────
  dynamic "statement" {
    for_each = length(var.admin_principals) > 0 ? [1] : []
    content {
      sid     = "AllowAdminFullAccess"
      effect  = "Allow"
      actions = ["s3:*"]
      resources = [
        local.bucket_arn,
        "${local.bucket_arn}/*",
      ]

      principals {
        type        = "AWS"
        identifiers = var.admin_principals
      }
    }
  }
}

# ===========================================================================
# IAM Role: Read role
# ===========================================================================

resource "aws_iam_role" "reader" {
  name        = var.reader_role_name
  description = "Read-only access to S3 bucket ${var.bucket_name}"

  assume_role_policy = data.aws_iam_policy_document.reader_trust.json

  tags = {
    Purpose = "s3-read"
    Bucket  = var.bucket_name
  }
}

data "aws_iam_policy_document" "reader_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = var.reader_trusted_principals
    }
  }
}

resource "aws_iam_policy" "reader" {
  name        = "${var.bucket_name}-read-policy"
  description = "Read access to S3 bucket ${var.bucket_name}"

  policy = data.aws_iam_policy_document.reader_permissions.json
}

data "aws_iam_policy_document" "reader_permissions" {
  # Retrieve and download objects
  statement {
    sid     = "ReadObjects"
    effect  = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:GetObjectTagging",
      "s3:GetObjectVersionTagging",
    ]
    resources = ["${local.bucket_arn}/*"]
  }

  # Bucket level: list contents
  statement {
    sid     = "ListBucket"
    effect  = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:ListBucketVersions",
      "s3:GetBucketLocation",
      "s3:GetBucketVersioning",
    ]
    resources = [local.bucket_arn]
  }
}

resource "aws_iam_role_policy_attachment" "reader" {
  role       = aws_iam_role.reader.name
  policy_arn = aws_iam_policy.reader.arn
}

# ===========================================================================
# IAM Role: Write role
# ===========================================================================

resource "aws_iam_role" "writer" {
  name        = var.writer_role_name
  description = "Write access to S3 bucket ${var.bucket_name}"

  assume_role_policy = data.aws_iam_policy_document.writer_trust.json

  tags = {
    Purpose = "s3-write"
    Bucket  = var.bucket_name
  }
}

data "aws_iam_policy_document" "writer_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = var.writer_trusted_principals
    }
  }
}

resource "aws_iam_policy" "writer" {
  name        = "${var.bucket_name}-write-policy"
  description = "Write access to S3 bucket ${var.bucket_name}"

  policy = data.aws_iam_policy_document.writer_permissions.json
}

data "aws_iam_policy_document" "writer_permissions" {
  # Write and delete objects
  statement {
    sid     = "WriteObjects"
    effect  = "Allow"
    actions = [
      "s3:PutObject",
      "s3:PutObjectTagging",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${local.bucket_arn}/*"]
  }

  # Read objects (write role can also read — the inverse is not necessarily needed)
  statement {
    sid     = "ReadObjects"
    effect  = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:GetObjectTagging",
    ]
    resources = ["${local.bucket_arn}/*"]
  }

  # Bucket level
  statement {
    sid     = "ListBucket"
    effect  = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:ListBucketVersions",
      "s3:ListBucketMultipartUploads",
      "s3:GetBucketLocation",
      "s3:GetBucketVersioning",
    ]
    resources = [local.bucket_arn]
  }
}

resource "aws_iam_role_policy_attachment" "writer" {
  role       = aws_iam_role.writer.name
  policy_arn = aws_iam_policy.writer.arn
}
