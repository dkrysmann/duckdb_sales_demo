# ---------------------------------------------------------------------------
# main.tf — S3-bucket afgeschermd tot twee IAM-rollen
#
# Architectuur:
#   • S3-bucket met versleuteling (AES-256) en blokkade van openbare toegang
#   • Bucketbeleid: WEIGER alles voor iedereen behalve leesrol, schrijfrol
#     en het AWS-account zelf (om vergrendeling te voorkomen)
#   • Leesrol: GetObject, ListBucket, GetBucketLocation
#   • Schrijfrol: PutObject, DeleteObject, ListBucket, GetObject, GetBucketLocation
#   • Geen gedeeld wachtwoord of toegangssleutel — uitsluitend IAM-rollen
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
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

# Haal het huidige AWS-account-ID op (nodig voor ARN-constructie)
data "aws_caller_identity" "current" {}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  reader_arn    = "arn:aws:iam::${local.account_id}:role/${var.reader_role_name}"
  writer_arn    = "arn:aws:iam::${local.account_id}:role/${var.writer_role_name}"
  account_root  = "arn:aws:iam::${local.account_id}:root"
  bucket_arn    = "arn:aws:s3:::${var.bucket_name}"
}

# ===========================================================================
# S3-bucket
# ===========================================================================

resource "aws_s3_bucket" "this" {
  bucket        = var.bucket_name
  force_destroy = var.force_destroy

  tags = {
    Name = var.bucket_name
  }
}

# --- Versiebeheer ----------------------------------------------------------
resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id

  versioning_configuration {
    status = var.versioning_enabled ? "Enabled" : "Suspended"
  }
}

# --- Versleuteling (server-side, AES-256) ----------------------------------
resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# --- Blokkeer ALLE openbare toegang ----------------------------------------
resource "aws_s3_bucket_public_access_block" "this" {
  bucket = aws_s3_bucket.this.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- Toegangslogs (optioneel) ----------------------------------------------
resource "aws_s3_bucket_logging" "this" {
  count = var.log_bucket != "" ? 1 : 0

  bucket        = aws_s3_bucket.this.id
  target_bucket = var.log_bucket
  target_prefix = "${var.bucket_name}/"
}

# ===========================================================================
# Bucketbeleid — weiger iedereen behalve leesrol, schrijfrol en accountroot
#
# Toelichting op de deny-aanpak:
#   ArnNotLike + Effect=Deny zorgt dat AWS ELKE aanvraag weigert
#   waarvan het principal-ARN niet overeenkomt met de drie uitzonderingen.
#   Dit is stabieler dan een positief Allow-only beleid, omdat het ook
#   acties blokkeert die via andere mechanismen (bijv. resource-based Allow
#   via organisatiebeleid) zouden kunnen binnenkomen.
# ===========================================================================

resource "aws_s3_bucket_policy" "this" {
  bucket = aws_s3_bucket.this.id

  # Wacht tot de publieke-toegangsblokkering is ingesteld
  depends_on = [aws_s3_bucket_public_access_block.this]

  policy = data.aws_iam_policy_document.bucket_policy.json
}

data "aws_iam_policy_document" "bucket_policy" {

  # ── Statement 1: verplicht HTTPS ────────────────────────────────────────
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

  # ── Statement 2: weiger iedereen BEHALVE de twee rollen + accountroot ───
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
      values = [
        local.reader_arn,
        local.writer_arn,
        local.account_root,    # voorkomt volledige vergrendeling van het account
      ]
    }
  }
}

# ===========================================================================
# IAM-rol: Leesrol
# ===========================================================================

resource "aws_iam_role" "reader" {
  name        = var.reader_role_name
  description = "Alleen-lezen toegang tot S3-bucket ${var.bucket_name}"

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
  description = "Leestoegang tot S3-bucket ${var.bucket_name}"

  policy = data.aws_iam_policy_document.reader_permissions.json
}

data "aws_iam_policy_document" "reader_permissions" {
  # Objecten ophalen en weergeven
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

  # Bucket-niveau: inhoud opvragen
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
# IAM-rol: Schrijfrol
# ===========================================================================

resource "aws_iam_role" "writer" {
  name        = var.writer_role_name
  description = "Schrijftoegang tot S3-bucket ${var.bucket_name}"

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
  description = "Schrijftoegang tot S3-bucket ${var.bucket_name}"

  policy = data.aws_iam_policy_document.writer_permissions.json
}

data "aws_iam_policy_document" "writer_permissions" {
  # Objecten schrijven en verwijderen
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

  # Objecten lezen (schrijver mag ook lezen — omgekeerde is niet per se nodig)
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

  # Bucket-niveau
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
