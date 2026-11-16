# WARNING: Do not run terraform apply against the shared bucket igtl-hr-analytics-ayush-2026 or the
# existing Glue database hr_analytics. It would conflict with and could overwrite a colleague's
# tables. This environment was set up via the AWS console instead.
#
# =====================================================================
# HR Onboarding on AWS: S3 (one folder per employee + HR tables), an EC2 instance running the
# onboarding portal (8000) and the HR portal (8001), IAM role, ECR, Glue/Athena.
#
#   terraform init
#   terraform apply -var-file=my.tfvars      (or just edit the defaults below)
#
# 1. EDIT THE VARIABLES IN SECTION 1: every name you may want to change is there.
# 2. After `apply`, build and push the image (the `next_steps` output prints the commands).
# =====================================================================

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}


# =====================================================================
# 1. VARIABLES: REPLACE THESE WITH YOUR OWN NAMES
# =====================================================================

variable "aws_region" {
  description = "Region everything is created in"
  type        = string
  default     = "ap-south-1"
}

variable "aws_profile" {
  description = "AWS CLI profile Terraform uses (null = default credentials / environment)"
  type        = string
  default     = null
}

variable "project_name" {
  description = "Prefix for the names below that you do not set yourself"
  type        = string
  default     = "hr-onboarding"
}

# ---- S3 ----------------------------------------------------------------
variable "s3_bucket_name" {
  description = "Main bucket: employees/<id>/ folders, hr/ tables, Athena results. Must be globally unique."
  type        = string
  default     = "your-company-hr-onboarding"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{2,62}$", var.s3_bucket_name))
    error_message = "Bucket names are 3-63 characters: lowercase letters, digits, dots and hyphens."
  }
}

variable "s3_restricted_bucket_name" {
  description = "Second bucket for the bank and sensitive (PAN/Aadhaar) tables. Globally unique."
  type        = string
  default     = "your-company-hr-onboarding-restricted"
}

variable "s3_kms_key_arn" {
  description = "KMS key ARN for encrypting objects (null = S3-managed AES256)"
  type        = string
  default     = null
}

variable "s3_force_destroy" {
  description = "Let `terraform destroy` delete the buckets even when they hold employee data. Keep false."
  type        = bool
  default     = false
}

# ---- IAM ---------------------------------------------------------------
variable "create_iam_role" {
  description = "true: create the role below. false: use the existing role named in iam_role_name (it needs the S3, ECR and SSM access in section 3)."
  type        = bool
  default     = true
}

variable "iam_role_name" {
  description = "Name of the role the EC2 instance runs as (created, or existing when create_iam_role = false)"
  type        = string
  default     = "hr-onboarding-ec2-role"
}

variable "iam_instance_profile_name" {
  description = "Name of the instance profile that attaches the role to the EC2 instance"
  type        = string
  default     = "hr-onboarding-ec2-profile"
}

# ---- EC2 ---------------------------------------------------------------
variable "ec2_instance_name" {
  description = "Name tag of the EC2 instance"
  type        = string
  default     = "hr-onboarding-server"
}

variable "ec2_instance_type" {
  type    = string
  default = "t3.small"
}

variable "ec2_key_pair_name" {
  description = "Existing EC2 key pair for SSH (null = no SSH; use SSM Session Manager)"
  type        = string
  default     = null
}

variable "vpc_id" {
  description = "VPC to use (null = the default VPC)"
  type        = string
  default     = null
}

variable "subnet_id" {
  description = "Public subnet for the instance (null = first subnet of the VPC). The instance needs internet access to reach the Gemini API."
  type        = string
  default     = null
}

variable "security_group_name" {
  type    = string
  default = "hr-onboarding-sg"
}

variable "onboarding_allowed_cidr" {
  description = "Who can open the employee onboarding portal (port 8000)"
  type        = string
  default     = "0.0.0.0/0"
}

variable "hr_allowed_cidr" {
  description = "Who can open the HR portal (port 8001): set this to your office / VPN range. It carries PAN, Aadhaar and bank details."
  type        = string
  default     = "203.0.113.0/24" # example range: replace
}

variable "ssh_allowed_cidr" {
  description = "Who can SSH (only used when ec2_key_pair_name is set)"
  type        = string
  default     = "203.0.113.0/24" # example range: replace
}

# ---- Container image / secrets ---------------------------------------------
variable "ecr_repository_name" {
  type    = string
  default = "hr-onboarding"
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "gemini_api_key" {
  description = "Gemini API key for document extraction. Pass with TF_VAR_gemini_api_key or a tfvars file; never commit it."
  type        = string
  sensitive   = true
}

variable "gemma_model" {
  type    = string
  default = "gemma-4-26b-a4b-it"
}

variable "hr_user" {
  description = "Login for the HR portal"
  type        = string
  default     = "hr"
}

variable "hr_password" {
  description = "Password for the HR portal"
  type        = string
  sensitive   = true
}

variable "ssm_parameter_prefix" {
  description = "Secrets are stored in SSM Parameter Store under this path"
  type        = string
  default     = "/hr-onboarding"
}

# ---- Glue / Athena -------------------------------------------------------------
variable "glue_database_name" {
  type    = string
  default = "hr_analytics"
}

variable "athena_workgroup_name" {
  type    = string
  default = "hr-onboarding"
}

variable "tags" {
  type    = map(string)
  default = { Project = "hr-onboarding" }
}


# =====================================================================
# 2. PROVIDER AND LOOKUPS
# =====================================================================

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile

  default_tags {
    tags = var.tags
  }
}

data "aws_caller_identity" "current" {}

data "aws_vpc" "selected" {
  id      = var.vpc_id
  default = var.vpc_id == null ? true : null
}

data "aws_subnets" "selected" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.selected.id]
  }
}

# Latest Amazon Linux 2023 (Docker and the AWS CLI come from its repositories)
data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  subnet_id    = coalesce(var.subnet_id, tolist(data.aws_subnets.selected.ids)[0])
  account_id   = data.aws_caller_identity.current.account_id
  ecr_registry = "${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
  image_uri    = "${local.ecr_registry}/${var.ecr_repository_name}:${var.image_tag}"
  buckets      = { main = var.s3_bucket_name, restricted = var.s3_restricted_bucket_name }
}


# =====================================================================
# 3. S3 BUCKETS
# =====================================================================

resource "aws_s3_bucket" "this" {
  for_each      = local.buckets
  bucket        = each.value
  force_destroy = var.s3_force_destroy
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each                = aws_s3_bucket.this
  bucket                  = each.value.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.s3_kms_key_arn == null ? "AES256" : "aws:kms"
      kms_master_key_id = var.s3_kms_key_arn
    }
  }
}

# Refuse any request that is not HTTPS
resource "aws_s3_bucket_policy" "tls_only" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [each.value.arn, "${each.value.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.this]
}


# =====================================================================
# 4. IAM ROLE FOR THE EC2 INSTANCE
# =====================================================================

resource "aws_iam_role" "ec2" {
  count = var.create_iam_role ? 1 : 0
  name  = var.iam_role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

data "aws_iam_role" "existing" {
  count = var.create_iam_role ? 0 : 1
  name  = var.iam_role_name
}

locals {
  role_name = var.create_iam_role ? aws_iam_role.ec2[0].name : data.aws_iam_role.existing[0].name
}

resource "aws_iam_role_policy" "app" {
  count = var.create_iam_role ? 1 : 0
  name  = "hr-onboarding-app"
  role  = aws_iam_role.ec2[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid      = "ObjectsInBothBuckets"
          Effect   = "Allow"
          Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
          Resource = [for bucket in aws_s3_bucket.this : "${bucket.arn}/*"]
        },
        {
          Sid      = "ListBothBuckets"
          Effect   = "Allow"
          Action   = "s3:ListBucket"
          Resource = [for bucket in aws_s3_bucket.this : bucket.arn]
        },
        {
          Sid      = "PullImage"
          Effect   = "Allow"
          Action   = ["ecr:GetAuthorizationToken"]
          Resource = "*"
        },
        {
          Sid      = "PullImageLayers"
          Effect   = "Allow"
          Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
          Resource = aws_ecr_repository.app.arn
        },
        {
          Sid      = "ReadSecrets"
          Effect   = "Allow"
          Action   = ["ssm:GetParameter", "ssm:GetParameters"]
          Resource = "arn:aws:ssm:${var.aws_region}:${local.account_id}:parameter${var.ssm_parameter_prefix}/*"
        },
      ],
      var.s3_kms_key_arn == null ? [] : [{
        Sid      = "UseKmsKey"
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = var.s3_kms_key_arn
      }]
    )
  })
}

# Lets you open a shell with Session Manager instead of SSH
resource "aws_iam_role_policy_attachment" "ssm_core" {
  count      = var.create_iam_role ? 1 : 0
  role       = aws_iam_role.ec2[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ec2" {
  name = var.iam_instance_profile_name
  role = local.role_name
}


# =====================================================================
# 5. ECR AND SECRETS
# =====================================================================

resource "aws_ecr_repository" "app" {
  name                 = var.ecr_repository_name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ssm_parameter" "gemini_api_key" {
  name  = "${var.ssm_parameter_prefix}/gemini_api_key"
  type  = "SecureString"
  value = var.gemini_api_key
}

resource "aws_ssm_parameter" "hr_password" {
  name  = "${var.ssm_parameter_prefix}/hr_password"
  type  = "SecureString"
  value = var.hr_password
}


# =====================================================================
# 6. EC2 INSTANCE: both portals as containers
# =====================================================================

resource "aws_security_group" "app" {
  name        = var.security_group_name
  description = "HR onboarding portals"
  vpc_id      = data.aws_vpc.selected.id

  ingress {
    description = "Onboarding portal"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.onboarding_allowed_cidr]
  }

  ingress {
    description = "HR portal"
    from_port   = 8001
    to_port     = 8001
    protocol    = "tcp"
    cidr_blocks = [var.hr_allowed_cidr]
  }

  dynamic "ingress" {
    for_each = var.ec2_key_pair_name == null ? [] : [1]
    content {
      description = "SSH"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = [var.ssh_allowed_cidr]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "app" {
  ami                         = data.aws_ssm_parameter.al2023_ami.value
  instance_type               = var.ec2_instance_type
  subnet_id                   = local.subnet_id
  vpc_security_group_ids      = [aws_security_group.app.id]
  iam_instance_profile        = aws_iam_instance_profile.ec2.name
  key_name                    = var.ec2_key_pair_name
  associate_public_ip_address = true

  # IMDSv2 only
  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_size = 20
    encrypted   = true
  }

  # Runs once at first boot. The image may not be pushed yet, so it waits for it.
  user_data = <<-EOT
    #!/bin/bash
    set -x
    dnf install -y docker
    systemctl enable --now docker

    REGION=${var.aws_region}
    aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin ${local.ecr_registry}
    until docker pull ${local.image_uri}; do
      echo "image not in ECR yet, retrying in 30s"; sleep 30
      aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin ${local.ecr_registry}
    done

    GEMINI_API_KEY=$(aws ssm get-parameter --region $REGION --with-decryption --name ${var.ssm_parameter_prefix}/gemini_api_key --query Parameter.Value --output text)
    HR_PASSWORD=$(aws ssm get-parameter --region $REGION --with-decryption --name ${var.ssm_parameter_prefix}/hr_password --query Parameter.Value --output text)
    PUBLIC_IP=$(curl -s -H "X-aws-ec2-metadata-token: $(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" http://169.254.169.254/latest/meta-data/public-ipv4)

    COMMON="-e STORAGE_BACKEND=s3 -e S3_BUCKET=${var.s3_bucket_name} -e S3_RESTRICTED_BUCKET=${var.s3_restricted_bucket_name} -e AWS_REGION=$REGION ${var.s3_kms_key_arn == null ? "" : "-e S3_KMS_KEY_ID=${var.s3_kms_key_arn}"}"

    docker run -d --name onboarding --restart always -p 8000:8000 $COMMON \
      -e PORT=8000 -e GEMINI_API_KEY="$GEMINI_API_KEY" -e GEMMA_MODEL=${var.gemma_model} \
      ${local.image_uri} python react_onboard.py

    docker run -d --name hr --restart always -p 8001:8001 $COMMON \
      -e PORT=8001 -e HR_USER=${var.hr_user} -e HR_PASSWORD="$HR_PASSWORD" \
      -e ONBOARDING_URL=http://$PUBLIC_IP:8000 \
      ${local.image_uri} python hr_info.py
  EOT

  user_data_replace_on_change = true

  tags = {
    Name = var.ec2_instance_name
  }

  depends_on = [aws_ssm_parameter.gemini_api_key, aws_ssm_parameter.hr_password]
}


# =====================================================================
# 7. GLUE + ATHENA: the Data/sources_hr tables over S3
# =====================================================================

locals {
  # table => [restricted?, [column name, glue type] ...]  (same schema as athena_tables.sql)
  hr_tables = {
    employees = { restricted = false, columns = [
      ["employee_id", "string"], ["full_name", "string"], ["work_email", "string"], ["work_phone", "string"],
      ["department", "string"], ["designation", "string"], ["manager_id", "string"], ["employment_status", "string"],
      ["work_location", "string"], ["work_type", "string"], ["date_of_birth", "date"], ["gender", "string"],
    ] }
    employment_details = { restricted = false, columns = [
      ["employee_id", "string"], ["date_of_joining", "date"], ["confirmation_date", "date"], ["employment_type", "string"],
      ["probation_end_date", "date"], ["contract_end_date", "date"], ["last_working_date", "date"],
      ["notice_period_days", "int"], ["exit_reason", "string"],
    ] }
    employee_education = { restricted = false, columns = [
      ["education_id", "string"], ["employee_id", "string"], ["highest_qualification", "string"],
      ["specialization", "string"], ["institution", "string"], ["graduation_year", "int"], ["certifications", "string"],
    ] }
    employee_compensation = { restricted = false, columns = [
      ["compensation_id", "string"], ["employee_id", "string"], ["annual_ctc_inr", "bigint"],
      ["monthly_gross_inr", "double"], ["basic_monthly_inr", "double"], ["effective_from", "date"],
      ["bonus_eligible", "string"], ["salary_band", "string"],
    ] }
    employee_bank_details = { restricted = true, columns = [
      ["employee_id", "string"], ["bank_name", "string"], ["account_number_placeholder", "string"],
      ["ifsc_placeholder", "string"], ["account_type", "string"], ["verification_status", "string"],
      ["effective_from", "date"],
    ] }
    employee_sensitive_details = { restricted = true, columns = [
      ["employee_id", "string"], ["personal_email", "string"], ["personal_phone_placeholder", "string"],
      ["current_address", "string"], ["pan_placeholder", "string"], ["aadhaar_placeholder", "string"],
      ["emergency_contact_name", "string"], ["emergency_contact_phone_placeholder", "string"],
      ["sensitive_data_note", "string"],
    ] }
    leave_records = { restricted = false, columns = [
      ["leave_id", "string"], ["employee_id", "string"], ["leave_type", "string"], ["start_date", "date"],
      ["end_date", "date"], ["days_requested", "int"], ["approval_status", "string"],
      ["approver_employee_id", "string"], ["reason_category", "string"],
    ] }
    attendance = { restricted = false, columns = [
      ["attendance_id", "string"], ["employee_id", "string"], ["attendance_date", "date"],
      ["check_in_time", "string"], ["check_out_time", "string"], ["attendance_status", "string"],
      ["work_mode", "string"],
    ] }
  }
}

resource "aws_glue_catalog_database" "hr" {
  name = var.glue_database_name
}

resource "aws_glue_catalog_table" "hr" {
  for_each      = local.hr_tables
  name          = each.key
  database_name = aws_glue_catalog_database.hr.name
  table_type    = "EXTERNAL_TABLE"
  parameters    = { classification = "parquet", EXTERNAL = "TRUE" }

  storage_descriptor {
    location      = "s3://${each.value.restricted ? var.s3_restricted_bucket_name : var.s3_bucket_name}/hr/${each.key}/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    dynamic "columns" {
      for_each = each.value.columns
      content {
        name = columns.value[0]
        type = columns.value[1]
      }
    }
  }
}

resource "aws_athena_workgroup" "hr" {
  name = var.athena_workgroup_name

  configuration {
    result_configuration {
      output_location = "s3://${var.s3_bucket_name}/athena-results/"
    }
  }

  depends_on = [aws_s3_bucket.this]
}


# =====================================================================
# 8. OUTPUTS
# =====================================================================

output "onboarding_url" {
  value = "http://${aws_instance.app.public_ip}:8000"
}

output "hr_portal_url" {
  value = "http://${aws_instance.app.public_ip}:8001"
}

output "next_steps" {
  value = <<-EOT
    1. Build and push the image (from the project folder):
         aws ecr get-login-password --region ${var.aws_region} | docker login --username AWS --password-stdin ${local.ecr_registry}
         docker build -t ${local.image_uri} .
         docker push ${local.image_uri}
       The instance is already waiting for it and starts both portals within a minute.
    2. Move the existing local data (set STORAGE_BACKEND=s3, S3_BUCKET=${var.s3_bucket_name},
       S3_RESTRICTED_BUCKET=${var.s3_restricted_bucket_name}, AWS_REGION=${var.aws_region}), then:
         python sync_tables.py --from-local
    3. HR portal login: user "${var.hr_user}", password from var.hr_password.
  EOT
}
