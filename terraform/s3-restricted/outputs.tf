# ---------------------------------------------------------------------------
# outputs.tf — uitvoerwaarden na terraform apply
# ---------------------------------------------------------------------------

output "bucket_id" {
  description = "Naam (ID) van de aangemaakte S3-bucket"
  value       = aws_s3_bucket.this.id
}

output "bucket_arn" {
  description = "ARN van de S3-bucket"
  value       = aws_s3_bucket.this.arn
}

output "bucket_region" {
  description = "AWS-regio van de bucket"
  value       = aws_s3_bucket.this.region
}

output "reader_role_arn" {
  description = "ARN van de IAM-leesrol"
  value       = aws_iam_role.reader.arn
}

output "writer_role_arn" {
  description = "ARN van de IAM-schrijfrol"
  value       = aws_iam_role.writer.arn
}

output "reader_policy_arn" {
  description = "ARN van het IAM-leesbeleid"
  value       = aws_iam_policy.reader.arn
}

output "writer_policy_arn" {
  description = "ARN van het IAM-schrijfbeleid"
  value       = aws_iam_policy.writer.arn
}

# Commando's om de rollen te gebruiken via AWS CLI
output "usage_examples" {
  description = "Voorbeeldcommando's om de rollen te gebruiken"
  value = <<-EOT
    # Leesrol aannemen:
    aws sts assume-role \
      --role-arn ${aws_iam_role.reader.arn} \
      --role-session-name read-session

    # Schrijfrol aannemen:
    aws sts assume-role \
      --role-arn ${aws_iam_role.writer.arn} \
      --role-session-name write-session

    # Bestand uploaden als schrijfrol (via AWS CLI profiel):
    aws s3 cp bestand.csv s3://${aws_s3_bucket.this.id}/ --profile schrijfrol

    # Inhoud weergeven als leesrol:
    aws s3 ls s3://${aws_s3_bucket.this.id}/ --profile leesrol
  EOT
}
