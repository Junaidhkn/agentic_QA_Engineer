# ECS Fargate services (operations §1): web, api, three Celery worker pools, the scheduler
# (one task, never two), the outbox relay, the customer OTLP gateway and the platform metrics
# collector. Rolling deploys keep 100 % healthy, roll back on failed health checks (circuit
# breaker) and on the ALB 5xx alarm within the deployment; migrations run as a one-off task
# before a deploy (expand → migrate → contract).

variable "telemetry" {
  description = "Endpoints of the LGTM stack (customer evidence + platform tenant)"
  type = object({
    mimir_url          = string
    tempo_url          = string
    loki_url           = string
    otel_collector_url = string
  })
}
variable "canary" {
  description = "Synthetic canary project/target ids (operations §6); null until the canary org exists"
  type = object({
    project_id = string
    target_id  = string
  })
  default = null
}
variable "collector_image" {
  type    = string
  default = "otel/opentelemetry-collector-contrib:0.112.0"
}

locals {
  namespace      = "${var.name}.internal"
  worker_command = ["celery", "-A", "reliability.main_worker:celery_app", "worker", "--without-gossip", "--without-mingle"]
  service_defaults = {
    web            = { cpu = 512, memory = 1024, min = 2, max = 6, image = "web", port = 3000, command = null, metrics = false }
    api            = { cpu = 1024, memory = 2048, min = 2, max = 8, image = "control-plane", port = 8000, metrics = false, command = ["uvicorn", "reliability.main_api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"] }
    worker-default = { cpu = 1024, memory = 2048, min = 2, max = 8, image = "control-plane", port = null, metrics = true, command = concat(local.worker_command, ["-Q", "default", "--concurrency", "4"]) }
    worker-agents  = { cpu = 2048, memory = 4096, min = 2, max = 8, image = "control-plane", port = null, metrics = true, command = concat(local.worker_command, ["-Q", "agents", "--concurrency", "8"]) }
    worker-github  = { cpu = 512, memory = 1024, min = 1, max = 2, image = "control-plane", port = null, metrics = true, command = concat(local.worker_command, ["-Q", "github", "--concurrency", "2"]) }
    scheduler      = { cpu = 256, memory = 512, min = 1, max = 1, image = "control-plane", port = null, metrics = false, command = ["celery", "-A", "reliability.main_worker:celery_app", "beat", "--schedule", "/tmp/celerybeat-schedule"] }
    outbox-relay   = { cpu = 256, memory = 512, min = 1, max = 2, image = "control-plane", port = null, metrics = false, command = ["reliability", "outbox-relay"] }
    otlp-gateway   = { cpu = 1024, memory = 2048, min = 2, max = 6, image = "control-plane", port = 4319, metrics = false, command = ["uvicorn", "reliability.otlp_gateway:app", "--host", "0.0.0.0", "--port", "4319"] }
  }
  svc = {
    for k, d in local.service_defaults : k => merge(d, {
      cpu    = coalesce(try(var.services[k].cpu, null), d.cpu)
      memory = coalesce(try(var.services[k].memory, null), d.memory)
      min    = coalesce(try(var.services[k].min, null), d.min)
      max    = coalesce(try(var.services[k].max, null), d.max)
    })
  }

  secret_names = [
    "database-url", "database-system-url", "database-migration-url", "redis-url", "api-key-pepper",
    "token-signing-key", "runner-signing-key", "metrics-token", "anthropic-api-key", "github-private-key",
    "github-webhook-secret", "github-client-secret", "sentry-dsn",
  ]
  backend_env = merge(
    {
      RE_ENV                = var.environment
      RE_API_BASE_URL       = "https://${local.hosts.api}"
      RE_WEB_BASE_URL       = "https://${local.hosts.app}"
      RE_CORS_ORIGINS       = jsonencode(["https://${local.hosts.app}"])
      RE_OBJECT_STORE       = "s3"
      RE_S3_BUCKET          = aws_s3_bucket.artifacts.id
      RE_S3_REGION          = data.aws_region.current.name
      RE_KMS                = "aws"
      RE_KMS_KEY_ID         = aws_kms_key.data.arn
      RE_DEV_AUTH_ENABLED   = "false"
      RE_LLM_PROVIDER       = "anthropic"
      RE_EMAIL_PROVIDER     = "ses"
      RE_OTLP_PUBLIC_URL    = "https://${local.hosts.otlp}"
      RE_MIMIR_URL          = var.telemetry.mimir_url
      RE_TEMPO_URL          = var.telemetry.tempo_url
      RE_LOKI_URL           = var.telemetry.loki_url
      RE_OTEL_COLLECTOR_URL = var.telemetry.otel_collector_url
      RE_LOG_LEVEL          = "INFO"
    },
    var.canary == null ? {} : { RE_CANARY_PROJECT_ID = var.canary.project_id, RE_CANARY_TARGET_ID = var.canary.target_id },
  )
  backend_secrets = {
    RE_DATABASE_URL           = "database-url"
    RE_DATABASE_SYSTEM_URL    = "database-system-url"
    RE_DATABASE_MIGRATION_URL = "database-migration-url"
    RE_REDIS_URL              = "redis-url"
    RE_API_KEY_PEPPER         = "api-key-pepper"
    RE_TOKEN_SIGNING_KEY      = "token-signing-key"
    RE_RUNNER_SIGNING_KEY     = "runner-signing-key"
    RE_METRICS_TOKEN          = "metrics-token"
    RE_ANTHROPIC_API_KEY      = "anthropic-api-key"
    RE_GITHUB_PRIVATE_KEY     = "github-private-key"
    RE_GITHUB_WEBHOOK_SECRET  = "github-webhook-secret"
    RE_GITHUB_CLIENT_SECRET   = "github-client-secret"
    RE_SENTRY_DSN             = "sentry-dsn"
  }
}

# Values are written out-of-band (runbooks/README.md "First deploy"); Terraform never sees them.
resource "aws_secretsmanager_secret" "app" {
  for_each                = toset(local.secret_names)
  name                    = "${var.name}/${each.key}"
  kms_key_id              = aws_kms_key.data.arn
  recovery_window_in_days = 7
  tags                    = var.tags
}

resource "aws_ecs_cluster" "this" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
  tags = var.tags
}

resource "aws_service_discovery_private_dns_namespace" "this" {
  name = local.namespace
  vpc  = var.vpc_id
  tags = var.tags
}

resource "aws_service_discovery_service" "svc" {
  for_each = toset(["api", "worker-default", "worker-agents", "worker-github", "otlp-gateway"])
  name     = each.key
  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.this.id
    routing_policy = "MULTIVALUE"
    dns_records {
      type = "A"
      ttl  = 10
    }
  }
  health_check_custom_config {
    failure_threshold = 1
  }
  tags = var.tags
}

resource "aws_cloudwatch_log_group" "svc" {
  for_each          = toset(concat(keys(local.service_defaults), ["platform-collector", "migrate", "restore-drill"]))
  name              = "/ecs/${var.name}/${each.key}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

# --- IAM -------------------------------------------------------------------------------------
data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = concat([for s in aws_secretsmanager_secret.app : s.arn], [aws_db_instance.this.master_user_secret[0].secret_arn])
      },
      { Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.data.arn },
    ]
  })
}

resource "aws_iam_role" "app" {
  name               = "${var.name}-app"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "app" {
  role = aws_iam_role.app.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Artifacts"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
      },
      {
        Sid      = "AuditAnchorsWriteOnce"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.audit.arn}/*"
      },
      {
        Sid      = "EnvelopeKeys"
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = aws_kms_key.data.arn
      },
      { Sid = "Email", Effect = "Allow", Action = ["ses:SendEmail", "ses:SendRawEmail"], Resource = "*" },
    ]
  })
}

# --- Task definitions and services ------------------------------------------------------------
locals {
  secret_refs = [for env, key in local.backend_secrets : { name = env, valueFrom = aws_secretsmanager_secret.app[key].arn }]
  log_config = { for k in keys(aws_cloudwatch_log_group.svc) : k => {
    logDriver = "awslogs"
    options = {
      awslogs-group         = aws_cloudwatch_log_group.svc[k].name
      awslogs-region        = data.aws_region.current.name
      awslogs-stream-prefix = k
    }
  } }
}

resource "aws_ecs_task_definition" "svc" {
  for_each                 = local.svc
  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.app.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([
    merge(
      {
        name             = each.key
        image            = "${var.image_repository}/${each.value.image}:${var.image_tag}"
        essential        = true
        portMappings     = each.value.port == null && !each.value.metrics ? [] : [{ containerPort = each.value.port == null ? 9464 : each.value.port, protocol = "tcp" }]
        logConfiguration = local.log_config[each.key]
        linuxParameters  = { initProcessEnabled = true }
        stopTimeout      = 60
      },
      each.key == "web" ? {
        environment = [
          { name = "NEXT_PUBLIC_API_BASE_URL", value = "https://${local.hosts.api}" },
          { name = "NODE_ENV", value = "production" },
        ]
        } : {
        command = each.value.command
        environment = [for k, v in merge(local.backend_env, each.value.metrics ? {
          RE_METRICS_PORT          = "9464"
          PROMETHEUS_MULTIPROC_DIR = "/tmp/prometheus"
        } : {}) : { name = k, value = v }]
        secrets = local.secret_refs
      },
    )
  ])
  tags = var.tags
}

locals {
  lb_bindings = {
    web          = [{ tg = aws_lb_target_group.web.arn, port = 3000 }]
    api          = [{ tg = aws_lb_target_group.api.arn, port = 8000 }, { tg = aws_lb_target_group.api_internal.arn, port = 8000 }]
    otlp-gateway = [{ tg = aws_lb_target_group.otlp.arn, port = 4319 }, { tg = aws_lb_target_group.otlp_internal.arn, port = 4319 }]
  }
}

resource "aws_ecs_service" "svc" {
  for_each        = local.svc
  name            = each.key
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.svc[each.key].arn
  desired_count   = each.value.min
  launch_type     = "FARGATE"
  # The scheduler must never run twice (duplicate beats): stop the old task before the new one.
  deployment_minimum_healthy_percent = each.key == "scheduler" ? 0 : 100
  deployment_maximum_percent         = each.key == "scheduler" ? 100 : 200
  health_check_grace_period_seconds  = contains(keys(local.lb_bindings), each.key) ? 60 : null
  propagate_tags                     = "SERVICE"
  enable_execute_command             = false
  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  dynamic "alarms" {
    for_each = contains(["web", "api"], each.key) ? [1] : []
    content {
      alarm_names = [aws_cloudwatch_metric_alarm.alb_5xx.alarm_name]
      enable      = true
      rollback    = true
    }
  }
  dynamic "load_balancer" {
    for_each = lookup(local.lb_bindings, each.key, [])
    content {
      target_group_arn = load_balancer.value.tg
      container_name   = each.key
      container_port   = load_balancer.value.port
    }
  }
  dynamic "service_registries" {
    for_each = contains(keys(aws_service_discovery_service.svc), each.key) ? [1] : []
    content {
      registry_arn = aws_service_discovery_service.svc[each.key].arn
    }
  }
  lifecycle {
    ignore_changes = [desired_count] # owned by autoscaling
  }
  tags = var.tags
}

resource "aws_appautoscaling_target" "svc" {
  for_each           = { for k, v in local.svc : k => v if v.max > v.min }
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.svc[each.key].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = each.value.min
  max_capacity       = each.value.max
}

resource "aws_appautoscaling_policy" "cpu" {
  for_each           = aws_appautoscaling_target.svc
  name               = "${var.name}-${each.key}-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = each.value.service_namespace
  resource_id        = each.value.resource_id
  scalable_dimension = each.value.scalable_dimension
  target_tracking_scaling_policy_configuration {
    target_value       = 60
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

# --- Platform metrics collector (deploy/otel/platform-collector.yaml) ---------------------------
resource "aws_ecs_task_definition" "collector" {
  family                   = "${var.name}-platform-collector"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions = jsonencode([{
    name             = "platform-collector"
    image            = var.collector_image
    essential        = true
    command          = ["--config=env:OTEL_CONFIG"]
    logConfiguration = local.log_config["platform-collector"]
    environment = [
      { name = "OTEL_CONFIG", value = file("${path.module}/../../../../deploy/otel/platform-collector.yaml") },
      { name = "MIMIR_URL", value = var.telemetry.mimir_url },
      { name = "API_DISCOVERY_NAME", value = "api.${local.namespace}" },
      { name = "WORKER_DEFAULT_DISCOVERY_NAME", value = "worker-default.${local.namespace}" },
      { name = "WORKER_AGENTS_DISCOVERY_NAME", value = "worker-agents.${local.namespace}" },
      { name = "WORKER_GITHUB_DISCOVERY_NAME", value = "worker-github.${local.namespace}" },
    ]
    secrets = [{ name = "RE_METRICS_TOKEN", valueFrom = aws_secretsmanager_secret.app["metrics-token"].arn }]
  }])
  tags = var.tags
}

resource "aws_ecs_service" "collector" {
  name                               = "platform-collector"
  cluster                            = aws_ecs_cluster.this.id
  task_definition                    = aws_ecs_task_definition.collector.arn
  desired_count                      = 1
  launch_type                        = "FARGATE"
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  network_configuration {
    subnets         = var.private_subnet_ids
    security_groups = [aws_security_group.tasks.id]
  }
  tags = var.tags
}

# --- One-off tasks: migrations (run by CD before each deploy) and the weekly restore drill ----
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${var.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.app.arn
  container_definitions = jsonencode([{
    name             = "migrate"
    image            = "${var.image_repository}/control-plane:${var.image_tag}"
    essential        = true
    command          = ["alembic", "upgrade", "head"]
    logConfiguration = local.log_config["migrate"]
    environment      = [for k, v in local.backend_env : { name = k, value = v }]
    secrets          = local.secret_refs
  }])
  tags = var.tags
}

resource "aws_iam_role" "drill" {
  name               = "${var.name}-restore-drill"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "drill" {
  role = aws_iam_role.drill.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["rds:DescribeDBSnapshots", "rds:DescribeDBInstances"]
        Resource = "*"
      },
      {
        Sid    = "RestoreOnlyDrillInstances"
        Effect = "Allow"
        Action = ["rds:RestoreDBInstanceFromDBSnapshot", "rds:AddTagsToResource", "rds:DeleteDBInstance"]
        Resource = [
          "arn:aws:rds:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:db:${var.name}-drill-*",
          "arn:aws:rds:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:snapshot:rds:${aws_db_instance.this.identifier}-*",
          aws_db_subnet_group.this.arn,
          aws_db_parameter_group.pg16.arn,
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:CreateGrant", "kms:Decrypt", "kms:DescribeKey"]
        Resource = aws_kms_key.data.arn
      },
    ]
  })
}

resource "aws_ecs_task_definition" "drill" {
  family                   = "${var.name}-restore-drill"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.drill.arn
  container_definitions = jsonencode([{
    name      = "restore-drill"
    image     = "${var.image_repository}/control-plane:${var.image_tag}"
    essential = true
    command = [
      "reliability", "restore-drill", "--mode", "rds",
      "--rds-instance", aws_db_instance.this.identifier,
      "--drill-prefix", "${var.name}-drill",
      "--subnet-group", aws_db_subnet_group.this.name,
      "--security-group", aws_security_group.db.id,
      "--instance-class", "db.t4g.large",
    ]
    logConfiguration = local.log_config["restore-drill"]
    environment      = [for k, v in local.backend_env : { name = k, value = v }]
    secrets = concat(local.secret_refs, [
      { name = "RE_DRILL_DB_USER", valueFrom = "${aws_db_instance.this.master_user_secret[0].secret_arn}:username::" },
      { name = "RE_DRILL_DB_PASSWORD", valueFrom = "${aws_db_instance.this.master_user_secret[0].secret_arn}:password::" },
    ])
  }])
  tags = var.tags
}

resource "aws_iam_role" "scheduler" {
  name = "${var.name}-scheduler"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "scheduler.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecs:RunTask"], Resource = aws_ecs_task_definition.drill.arn_without_revision },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.execution.arn, aws_iam_role.drill.arn] },
    ]
  })
}

resource "aws_scheduler_schedule" "restore_drill" {
  name                         = "${var.name}-restore-drill"
  schedule_expression          = var.restore_drill_schedule
  schedule_expression_timezone = "UTC"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_ecs_cluster.this.arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.drill.arn_without_revision
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = var.private_subnet_ids
        security_groups  = [aws_security_group.tasks.id]
        assign_public_ip = false
      }
    }
    retry_policy {
      maximum_retry_attempts = 0 # a failed drill is a finding, not something to paper over
    }
  }
}
