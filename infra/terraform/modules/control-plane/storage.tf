# S3: artifacts (SSE-KMS, versioned, short noncurrent retention), audit anchors (Object Lock
# compliance mode, 1 year, replicated to the DR region), LGTM telemetry data (30 days), ALB
# access logs. All buckets block public access and refuse non-TLS requests.

locals {
  buckets = {
    artifacts = "${var.name}-artifacts-${data.aws_caller_identity.current.account_id}"
    audit     = "${var.name}-audit-anchors-${data.aws_caller_identity.current.account_id}"
    lgtm      = "${var.name}-lgtm-data-${data.aws_caller_identity.current.account_id}"
    alb_logs  = "${var.name}-alb-logs-${data.aws_caller_identity.current.account_id}"
  }
}

resource "aws_s3_bucket" "artifacts" {
  bucket = local.buckets.artifacts
  tags   = var.tags
}

resource "aws_s3_bucket" "audit" {
  bucket              = local.buckets.audit
  object_lock_enabled = true
  tags                = var.tags
}

resource "aws_s3_bucket" "lgtm" {
  bucket = local.buckets.lgtm
  tags   = var.tags
}

resource "aws_s3_bucket" "alb_logs" {
  bucket = local.buckets.alb_logs
  tags   = var.tags
}

locals {
  bucket_ids = {
    artifacts = aws_s3_bucket.artifacts.id
    audit     = aws_s3_bucket.audit.id
    lgtm      = aws_s3_bucket.lgtm.id
    alb_logs  = aws_s3_bucket.alb_logs.id
  }
  bucket_arns = {
    artifacts = aws_s3_bucket.artifacts.arn
    audit     = aws_s3_bucket.audit.arn
    lgtm      = aws_s3_bucket.lgtm.arn
    alb_logs  = aws_s3_bucket.alb_logs.arn
  }
}

resource "aws_s3_bucket_public_access_block" "all" {
  for_each                = local.bucket_ids
  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "all" {
  for_each = local.bucket_ids
  bucket   = each.value
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "versioned" {
  for_each = { artifacts = aws_s3_bucket.artifacts.id, audit = aws_s3_bucket.audit.id }
  bucket   = each.value
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "kms" {
  for_each = { artifacts = aws_s3_bucket.artifacts.id, audit = aws_s3_bucket.audit.id, lgtm = aws_s3_bucket.lgtm.id }
  bucket   = each.value
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

# ALB access logs only support SSE-S3.
resource "aws_s3_bucket_server_side_encryption_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "noncurrent-7d"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
  rule {
    # Backstop only: the retention job deletes artifacts at their expires_at (plan-dependent).
    id     = "backstop-400d"
    status = "Enabled"
    filter {}
    expiration {
      days = 400
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "lgtm" {
  bucket = aws_s3_bucket.lgtm.id
  rule {
    id     = "telemetry-30d"
    status = "Enabled"
    filter {}
    expiration {
      days = 30
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  rule {
    id     = "logs-90d"
    status = "Enabled"
    filter {}
    expiration {
      days = 90
    }
  }
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = 365
    }
  }
}

data "aws_elb_service_account" "this" {}

data "aws_iam_policy_document" "bucket" {
  for_each = local.bucket_arns
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [each.value, "${each.value}/*"]
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
  dynamic "statement" {
    for_each = each.key == "alb_logs" ? [1] : []
    content {
      sid       = "AlbAccessLogs"
      actions   = ["s3:PutObject"]
      resources = ["${each.value}/alb/*"]
      principals {
        type        = "AWS"
        identifiers = [data.aws_elb_service_account.this.arn]
      }
    }
  }
}

resource "aws_s3_bucket_policy" "all" {
  for_each = local.bucket_ids
  bucket   = each.value
  policy   = data.aws_iam_policy_document.bucket[each.key].json
}

# --- Audit anchors replicated to the DR region (Object Lock there too) --------------------
resource "aws_s3_bucket" "audit_dr" {
  provider            = aws.dr
  bucket              = "${local.buckets.audit}-dr"
  object_lock_enabled = true
  tags                = var.tags
}

resource "aws_s3_bucket_versioning" "audit_dr" {
  provider = aws.dr
  bucket   = aws_s3_bucket.audit_dr.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "audit_dr" {
  provider                = aws.dr
  bucket                  = aws_s3_bucket.audit_dr.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_object_lock_configuration" "audit_dr" {
  provider = aws.dr
  bucket   = aws_s3_bucket.audit_dr.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = 365
    }
  }
}

resource "aws_iam_role" "replication" {
  name = "${var.name}-audit-replication"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "s3.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "replication" {
  role = aws_iam_role.replication.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
        Resource = aws_s3_bucket.audit.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObjectVersionForReplication", "s3:GetObjectVersionAcl", "s3:GetObjectRetention", "s3:GetObjectLegalHold"]
        Resource = "${aws_s3_bucket.audit.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ReplicateObject", "s3:ReplicateDelete", "s3:ReplicateTags"]
        Resource = "${aws_s3_bucket.audit_dr.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = aws_kms_key.data.arn
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.dr.arn
      },
    ]
  })
}

resource "aws_s3_bucket_replication_configuration" "audit" {
  depends_on = [aws_s3_bucket_versioning.versioned, aws_s3_bucket_versioning.audit_dr]
  bucket     = aws_s3_bucket.audit.id
  role       = aws_iam_role.replication.arn
  rule {
    id     = "audit-anchors-to-dr"
    status = "Enabled"
    filter {}
    delete_marker_replication {
      status = "Disabled"
    }
    source_selection_criteria {
      sse_kms_encrypted_objects {
        status = "Enabled"
      }
    }
    destination {
      bucket        = aws_s3_bucket.audit_dr.arn
      storage_class = "STANDARD_IA"
      encryption_configuration {
        replica_kms_key_id = aws_kms_key.dr.arn
      }
    }
  }
}
