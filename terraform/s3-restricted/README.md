# Terraform — Beperkte S3-bucket

Maakt een AWS S3-bucket aan die volledig is afgeschermd van iedereen, behalve:

| Rol | Toegestane acties |
|---|---|
| **Leesrol** (`s3-reader`) | `GetObject`, `ListBucket`, `GetObjectVersion`, … |
| **Schrijfrol** (`s3-writer`) | `PutObject`, `DeleteObject`, `GetObject`, `ListBucket`, … |
| **AWS-accountroot** | Alles (noodfallback — voorkomt permanente vergrendeling) |

Alle andere principals — inclusief andere IAM-gebruikers, rollen en AWS-diensten
in hetzelfde account — worden geweigerd op bucketbeleids-niveau.

---

## Vereisten

| Vereiste | Versie |
|---|---|
| Terraform | ≥ 1.5.0 |
| AWS Provider | ~> 5.0 |
| AWS CLI | geconfigureerd met voldoende rechten om IAM en S3 te beheren |

---

## Structuur

```
terraform/s3-restricted/
├── main.tf                  ← bucket, beleid, IAM-rollen en -policies
├── variables.tf             ← alle invoerparameters
├── outputs.tf               ← bucket-ARN, rol-ARNs, voorbeeldcommando's
├── terraform.tfvars.example ← voorbeeldwaarden (kopieer naar terraform.tfvars)
└── .gitignore               ← sluit tfstate en tfvars uit
```

---

## Gebruik

### 1. Voorbereiding

```bash
cd terraform/s3-restricted

# Kopieer en pas het configuratiebestand aan
cp terraform.tfvars.example terraform.tfvars
# Bewerk terraform.tfvars met de juiste bucket-naam en principals
```

### 2. Initialiseren

```bash
terraform init
```

### 3. Plan bekijken

```bash
terraform plan
```

### 4. Toepassen

```bash
terraform apply
```

### 5. Opruimen

```bash
terraform destroy   # verwijdert bucket, rollen en beleid
```

> ⚠️ Bij `force_destroy = false` (standaard productie-instelling) blokkeert
> Terraform de `destroy` als de bucket niet leeg is. Verwijder eerst alle
> objecten of zet tijdelijk `force_destroy = true`.

---

## Beveiligingsarchitectuur

```
┌─────────────────────────────────────────────────────┐
│                   S3-bucket                         │
│                                                     │
│  ┌─ Bucketbeleid ──────────────────────────────┐    │
│  │                                             │    │
│  │  Statement 1: Weiger HTTP (alleen HTTPS)    │    │
│  │                                             │    │
│  │  Statement 2: Weiger * BEHALVE:             │    │
│  │    ✅ arn:aws:iam::ACCOUNT:role/s3-reader   │    │
│  │    ✅ arn:aws:iam::ACCOUNT:role/s3-writer   │    │
│  │    ✅ arn:aws:iam::ACCOUNT:root (fallback)  │    │
│  │                                             │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
│  ┌─ Blokkade openbare toegang (alle 4 vlaggen) ─┐   │
│  └──────────────────────────────────────────────┘   │
│                                                     │
│  ┌─ Versleuteling: AES-256 (SSE-S3) ─────────┐     │
│  └────────────────────────────────────────────┘     │
│                                                     │
│  ┌─ Versiebeheer (optioneel, standaard: aan) ──┐    │
│  └─────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────┘

  Leesrol                    Schrijfrol
  (s3-reader)                (s3-writer)
  ┌────────────┐             ┌────────────┐
  │GetObject   │             │PutObject   │
  │ListBucket  │             │DeleteObject│
  │GetObjectV. │             │GetObject   │
  └────────────┘             │ListBucket  │
                             └────────────┘
```

### Waarom `ArnNotLike` + `Effect: Deny`?

Een `Effect: Deny` met `ArnNotLike` is **sterker** dan alleen een `Effect: Allow`:

- Een `Deny` in een bucketbeleid overschrijft elke `Allow` in een
  identiteitsbeleid (IAM-rol of gebruikersbeleid).
- Zelfs als iemand later een identiteitsbeleid aanmaakt met
  `s3:GetObject Allow` op deze bucket, wordt die aanvraag alsnog geweigerd
  door het bucketbeleid.
- De enige uitzondering is de AWS-accountroot — die heeft altijd toegang
  als noodfallback, ook als het bucketbeleid per ongeluk onjuist is.

---

## Toegestane acties per rol

### Leesrol

| Actie | Niveau | Toelichting |
|---|---|---|
| `s3:GetObject` | Object | Objectinhoud downloaden |
| `s3:GetObjectVersion` | Object | Specifieke versie downloaden |
| `s3:GetObjectTagging` | Object | Tags van een object lezen |
| `s3:GetObjectVersionTagging` | Object | Tags van een versie lezen |
| `s3:ListBucket` | Bucket | Objecten in de bucket opvragen |
| `s3:ListBucketVersions` | Bucket | Versiegeschiedenis opvragen |
| `s3:GetBucketLocation` | Bucket | Regio van de bucket opvragen |
| `s3:GetBucketVersioning` | Bucket | Versiestatus opvragen |

### Schrijfrol (inclusief lees)

Alle acties van de leesrol, plus:

| Actie | Niveau | Toelichting |
|---|---|---|
| `s3:PutObject` | Object | Objecten uploaden |
| `s3:PutObjectTagging` | Object | Tags toevoegen of aanpassen |
| `s3:DeleteObject` | Object | Objecten verwijderen |
| `s3:DeleteObjectVersion` | Object | Specifieke versie verwijderen |
| `s3:AbortMultipartUpload` | Object | Mislukte uploads annuleren |
| `s3:ListMultipartUploadParts` | Object | Onderdelen van multipart-upload opvragen |
| `s3:ListBucketMultipartUploads` | Bucket | Lopende multipart-uploads opvragen |

---

## Variabelen

| Naam | Type | Standaard | Beschrijving |
|---|---|---|---|
| `aws_region` | string | `eu-west-1` | AWS-regio |
| `bucket_name` | string | — | Globaal unieke bucketnaam (verplicht) |
| `environment` | string | `prd` | Omgevingslabel voor tags |
| `project` | string | `sales-pipeline` | Projectnaam voor tags |
| `reader_trusted_principals` | list(string) | — | ARNs die de leesrol mogen aannemen (verplicht) |
| `writer_trusted_principals` | list(string) | — | ARNs die de schrijfrol mogen aannemen (verplicht) |
| `reader_role_name` | string | `s3-reader` | Naam van de leesrol |
| `writer_role_name` | string | `s3-writer` | Naam van de schrijfrol |
| `versioning_enabled` | bool | `true` | Versiebeheer aan/uit |
| `force_destroy` | bool | `false` | Bucket leegmaken bij destroy |
| `log_bucket` | string | `""` | Bucketnaam voor toegangslogs |

---

## Uitvoerwaarden

Na `terraform apply` zijn de volgende waarden beschikbaar via `terraform output`:

```bash
terraform output bucket_arn       # ARN van de bucket
terraform output reader_role_arn  # ARN van de leesrol
terraform output writer_role_arn  # ARN van de schrijfrol
terraform output usage_examples   # kant-en-klare AWS CLI-commando's
```

---

## Veelgestelde vragen

**Kan ik meerdere leesrollen toevoegen?**  
Voeg extra ARNs toe aan `reader_trusted_principals` — meer principals kunnen
de ene leesrol aannemen. Als aparte rollen nodig zijn, voeg dan extra
`aws_iam_role`-resources toe en pas het bucketbeleid aan.

**Kan de leesrol ook schrijven als ik per ongeluk een extra `Allow` toevoeg?**  
Nee. Het bucketbeleid bevat een expliciete `Deny` voor `s3:PutObject` enz.
voor de leesrol-ARN. Een `Deny` op bucketbeleidsniveau wint altijd van een
`Allow` in een identiteitsbeleid.

**Wat als ik AWS Organizations gebruik?**  
Voeg een derde `Statement` toe aan `data.aws_iam_policy_document.bucket_policy`
met een `aws:PrincipalOrgID`-conditie om toegang te beperken tot principals
binnen uw organisatie.

**Moet ik de staatbestanden opslaan in een externe backend?**  
Ja, voor productie. Voeg een `backend "s3"` blok toe aan `main.tf` of gebruik
Terraform Cloud. Sla `terraform.tfstate` nooit op in Git.
