# ---------------------------------------------------------------------------
# variables.tf — invoerparameters voor de beperkte S3-bucket
# ---------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS-regio waar de bucket wordt aangemaakt"
  type        = string
  default     = "eu-west-1"
}

variable "bucket_name" {
  description = "Globaal unieke naam van de S3-bucket (lowercase, geen underscores)"
  type        = string
}

variable "environment" {
  description = "Omgevingslabel (bijv. dev, tst, acc, prd) — wordt als tag toegevoegd"
  type        = string
  default     = "prd"
}

variable "project" {
  description = "Projectnaam — wordt als tag toegevoegd"
  type        = string
  default     = "sales-pipeline"
}

# ---------------------------------------------------------------------------
# IAM-rollen — vertrouwde principals (wie mag de rol aannemen)
# ---------------------------------------------------------------------------

variable "reader_trusted_principals" {
  description = <<-EOT
    Lijst van ARNs die de leesrol mogen aannemen.
    Voorbeeld: ["arn:aws:iam::123456789012:root", "arn:aws:iam::123456789012:user/alice"]
  EOT
  type        = list(string)
}

variable "writer_trusted_principals" {
  description = <<-EOT
    Lijst van ARNs die de schrijfrol mogen aannemen.
    Voorbeeld: ["arn:aws:iam::123456789012:root", "arn:aws:iam::123456789012:user/deploy-bot"]
  EOT
  type        = list(string)
}

variable "reader_role_name" {
  description = "Naam van de IAM-rol met leestoegang"
  type        = string
  default     = "s3-reader"
}

variable "writer_role_name" {
  description = "Naam van de IAM-rol met schrijftoegang"
  type        = string
  default     = "s3-writer"
}

# ---------------------------------------------------------------------------
# Bucket-opties
# ---------------------------------------------------------------------------

variable "versioning_enabled" {
  description = "Schakel S3-objectversiebeheer in (aanbevolen voor audittrails)"
  type        = bool
  default     = true
}

variable "force_destroy" {
  description = "Verwijder alle objecten automatisch bij 'terraform destroy'. Zet op FALSE in productie."
  type        = bool
  default     = false
}

variable "log_bucket" {
  description = "Naam van de S3-bucket voor toegangslogs. Leeglaten om logging uit te schakelen."
  type        = string
  default     = ""
}
