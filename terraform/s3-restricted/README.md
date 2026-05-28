# Terraform — Restricted S3 Bucket

Creates an AWS S3 bucket that is fully locked down to everyone except:

| Role | Permitted actions |
|---|---|
| **Read role** (`s3-reader`) | `GetObject`, `ListBucket`, `GetObjectVersion`, … |
| **Write role** (`s3-writer`) | `PutObject`, `DeleteObject`, `GetObject`, `ListBucket`, … |
| **AWS account root** | Everything (emergency fallback — prevents permanent lock-out) |

All other principals — including other IAM users, roles, and AWS services in the
same account — are denied at bucket-policy level.

---

## Requirements

| Requirement | Version |
|---|---|
| Terraform | ≥ 1.5.0 |
| AWS Provider | ~> 5.0 |
| AWS CLI | configured with sufficient rights to manage IAM and S3 |

---

## Structure

```
terraform/s3-restricted/
├── main.tf                    ← S3 bucket, bucket policy, S3 IAM roles
├── snowflake.tf               ← Snowflake provider, IAM role for Snowflake, storage integration, stages
├── snowflake_contracts.tf     ← CONTRACTS table (INFER_SCHEMA), CONTRACTS_PIPE (Snowpipe)
├── snowflake_shelly.tf        ← SHELLY_PWR table (fixed VARIANT schema)
├── snowflake_streaming_user.tf← SHELLY_STREAMER user + SHELLY_STREAMING_ROLE + grants
├── variables.tf               ← all input parameters
├── outputs.tf                 ← bucket ARN, role ARNs, Snowpipe SQS channel
├── terraform.tfvars.example   ← example values (copy to terraform.tfvars)
└── .gitignore                 ← excludes tfstate and tfvars
```

---

## Usage

### 1. Preparation

```bash
cd terraform/s3-restricted

# Copy and edit the configuration file
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with the correct bucket name and principals
```

### 2. Initialise

```bash
terraform init
```

### 3. Review the plan

```bash
terraform plan
```

### 4. Apply

```bash
terraform apply
```

### 5. Clean up

```bash
terraform destroy   # removes bucket, roles and policies
```

> ⚠️ With `force_destroy = false` (the default production setting), Terraform
> blocks `destroy` if the bucket is not empty. Either delete all objects first
> or temporarily set `force_destroy = true`.

---

## Security Architecture

```
┌─────────────────────────────────────────────────────┐
│                     S3 Bucket                       │
│                                                     │
│  ┌─ Bucket Policy ────────────────────────────┐     │
│  │                                             │     │
│  │  Statement 1: Deny HTTP (HTTPS only)        │     │
│  │                                             │     │
│  │  Statement 2: Deny * EXCEPT:               │     │
│  │    ✅ arn:aws:iam::ACCOUNT:role/s3-reader   │     │
│  │    ✅ arn:aws:iam::ACCOUNT:role/s3-writer   │     │
│  │    ✅ arn:aws:iam::ACCOUNT:root (fallback)  │     │
│  │                                             │     │
│  └─────────────────────────────────────────────┘     │
│                                                     │
│  ┌─ Public Access Block (all 4 flags) ──────────┐   │
│  └──────────────────────────────────────────────┘   │
│                                                     │
│  ┌─ Encryption: AES-256 (SSE-S3) ────────────┐     │
│  └────────────────────────────────────────────┘     │
│                                                     │
│  ┌─ Versioning (optional, default: on) ────────┐    │
│  └─────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────┘

  Read role                   Write role
  (s3-reader)                 (s3-writer)
  ┌────────────┐              ┌────────────┐
  │GetObject   │              │PutObject   │
  │ListBucket  │              │DeleteObject│
  │GetObjectV. │              │GetObject   │
  └────────────┘              │ListBucket  │
                              └────────────┘
```

### Why `ArnNotLike` + `Effect: Deny`?

An `Effect: Deny` with `ArnNotLike` is **stronger** than a plain `Effect: Allow`:

- A `Deny` in a bucket policy overrides any `Allow` in an identity policy (IAM role
  or user policy).
- Even if someone later creates an identity policy with `s3:GetObject Allow` on this
  bucket, that request is still denied by the bucket policy.
- The only exception is the AWS account root — it always has access as an emergency
  fallback, even if the bucket policy is accidentally misconfigured.

---

## Permitted Actions Per Role

### Read role

| Action | Level | Description |
|---|---|---|
| `s3:GetObject` | Object | Download object content |
| `s3:GetObjectVersion` | Object | Download a specific version |
| `s3:GetObjectTagging` | Object | Read object tags |
| `s3:GetObjectVersionTagging` | Object | Read version tags |
| `s3:ListBucket` | Bucket | List objects in the bucket |
| `s3:ListBucketVersions` | Bucket | List version history |
| `s3:GetBucketLocation` | Bucket | Get the bucket's region |
| `s3:GetBucketVersioning` | Bucket | Get versioning status |

### Write role (includes read)

All read role actions, plus:

| Action | Level | Description |
|---|---|---|
| `s3:PutObject` | Object | Upload objects |
| `s3:PutObjectTagging` | Object | Add or update tags |
| `s3:DeleteObject` | Object | Delete objects |
| `s3:DeleteObjectVersion` | Object | Delete a specific version |
| `s3:AbortMultipartUpload` | Object | Cancel failed uploads |
| `s3:ListMultipartUploadParts` | Object | List parts of a multipart upload |
| `s3:ListBucketMultipartUploads` | Bucket | List in-progress multipart uploads |

---

## Variables

**S3 variables**

| Name | Type | Default | Description |
|---|---|---|---|
| `aws_region` | string | `eu-central-1` | AWS region |
| `bucket_name` | string | — | Globally unique bucket name (required) |
| `environment` | string | `prd` | Environment label for tags |
| `project` | string | `sales-pipeline` | Project name for tags |
| `reader_trusted_principals` | list(string) | — | ARNs allowed to assume the read role (required) |
| `writer_trusted_principals` | list(string) | — | ARNs allowed to assume the write role (required) |
| `reader_role_name` | string | `s3-reader` | Name of the read role |
| `writer_role_name` | string | `s3-writer` | Name of the write role |
| `admin_principals` | list(string) | `[]` | IAM ARNs exempt from the bucket-wide deny and granted `s3:*` |
| `versioning_enabled` | bool | `true` | Enable/disable versioning |
| `force_destroy` | bool | `false` | Empty bucket on destroy |
| `log_bucket` | string | `""` | Bucket name for access logs |

**Snowflake variables**

| Name | Type | Default | Description |
|---|---|---|---|
| `snowflake_organization_name` | string | — | Org name (e.g. `AYWPDSQ`) |
| `snowflake_account_name` | string | — | Account name (e.g. `ABB81033`) |
| `snowflake_user` | string | — | Snowflake user for Terraform |
| `snowflake_private_key_path` | string | — | Path to `.p8` RSA private key |
| `snowflake_role` | string | `ACCOUNTADMIN` | Snowflake role for Terraform |
| `snowflake_integration_name` | string | — | Name of the storage integration |
| `snowflake_database` | string | — | Target database |
| `snowflake_schema` | string | — | Target schema |
| `snowflake_iam_role_name` | string | `darek-snowflake-access-role` | AWS IAM role assumed by Snowflake |
| `snowflake_aws_iam_user_arn` | string | — | Snowflake's AWS IAM user ARN (from `DESC INTEGRATION`) |
| `snowflake_external_id` | string | — | External ID for the trust condition — **sensitive, set via env var** |
| `snowflake_s3_bucket` | string | — | S3 bucket Snowflake is allowed to read |
| `snowflake_streaming_rsa_public_key` | string | — | RSA public key content for `SHELLY_STREAMER` (no PEM header/footer) |

---

## Output Values

After `terraform apply`, the following values are available via `terraform output`:

```bash
terraform output bucket_arn                        # ARN of the S3 bucket
terraform output reader_role_arn                   # ARN of the S3 read role
terraform output writer_role_arn                   # ARN of the S3 write role
terraform output usage_examples                    # ready-to-use AWS CLI commands
terraform output snowflake_role_arn                # ARN of the Snowflake IAM role
terraform output snowflake_storage_integration_name# Snowflake integration name
terraform output contracts_pipe_notification_channel # SQS ARN for S3 event notification
```

For full Snowflake resource details see [SNOWFLAKE_README.md](../../SNOWFLAKE_README.md).

---

## FAQ

**Can I add multiple read principals?**  
Add extra ARNs to `reader_trusted_principals` — more principals can then assume
the single read role. If separate roles are needed, add additional `aws_iam_role`
resources and update the bucket policy accordingly.

**Can the read role also write if I accidentally add an extra `Allow`?**  
No. The bucket policy contains an explicit `Deny` for `s3:PutObject` etc. for
any principal that is not the write role or account root. A `Deny` at bucket-policy
level always wins over an `Allow` in an identity policy.

**What if I use AWS Organizations?**  
Add a third `Statement` to `data.aws_iam_policy_document.bucket_policy` with an
`aws:PrincipalOrgID` condition to restrict access to principals within your
organisation.

**Should I store state files in a remote backend?**  
Yes, for production. Add a `backend "s3"` block to `main.tf` or use Terraform
Cloud. Never store `terraform.tfstate` in Git.
