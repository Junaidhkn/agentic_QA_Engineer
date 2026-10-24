# CloudWatch alarms for what the platform cannot report about itself (load balancers, RDS,
# Redis, backups). SLO burn rates, queues, canary and integrity live in the Mimir rules
# (deploy/observability/rules). Everything notifies one SNS topic → on-call.

resource "aws_sns_topic" "alarms" {
  name              = "${var.name}-alarms"
  kms_master_key_id = "alias/aws/sns"
  tags              = var.tags
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alarm_email == null ? 0 : 1
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

resource "aws_sns_topic_subscription" "pager" {
  count                  = var.pager_https_endpoint == null ? 0 : 1
  topic_arn              = aws_sns_topic.alarms.arn
  protocol               = "https"
  endpoint               = var.pager_https_endpoint
  endpoint_auto_confirms = true
}

locals {
  alarm_actions = [aws_sns_topic.alarms.arn]
  alb_dim       = { LoadBalancer = aws_lb.public.arn_suffix }
}

# Also the ECS deployment rollback alarm for web and api.
resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${var.name}-alb-5xx-rate"
  alarm_description   = "Public ALB: more than 1% of requests answered 5xx for 3 minutes"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 1
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  metric_query {
    id          = "rate"
    expression  = "100 * (FILL(t5xx, 0) + FILL(e5xx, 0)) / FILL(req, 1)"
    label       = "5xx %"
    return_data = true
  }
  metric_query {
    id = "t5xx"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_Target_5XX_Count"
      dimensions  = local.alb_dim
      period      = 60
      stat        = "Sum"
    }
  }
  metric_query {
    id = "e5xx"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_ELB_5XX_Count"
      dimensions  = local.alb_dim
      period      = 60
      stat        = "Sum"
    }
  }
  metric_query {
    id = "req"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "RequestCount"
      dimensions  = local.alb_dim
      period      = 60
      stat        = "Sum"
    }
  }
  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "alb_latency" {
  alarm_name          = "${var.name}-alb-p95-latency"
  alarm_description   = "Public ALB target p95 above 1 s for 10 minutes (includes SSE/long requests)"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  dimensions          = local.alb_dim
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "runner_path_unhealthy" {
  alarm_name          = "${var.name}-privatelink-unhealthy"
  alarm_description   = "Runner path (PrivateLink NLB → internal ALB) has no healthy target: no job can start"
  namespace           = "AWS/NetworkELB"
  metric_name         = "HealthyHostCount"
  dimensions          = { LoadBalancer = aws_lb.privatelink.arn_suffix, TargetGroup = aws_lb_target_group.privatelink.arn_suffix }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "api_tasks" {
  alarm_name          = "${var.name}-api-running-tasks"
  alarm_description   = "Fewer than 2 API tasks running (one per AZ minimum)"
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  dimensions          = { ClusterName = aws_ecs_cluster.this.name, ServiceName = "api" }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 2
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  tags                = var.tags
}

locals {
  rds_alarms = {
    cpu         = { metric = "CPUUtilization", stat = "Average", op = "GreaterThanThreshold", threshold = 80, periods = 3, desc = "RDS CPU above 80% for 15 minutes" }
    storage     = { metric = "FreeStorageSpace", stat = "Minimum", op = "LessThanThreshold", threshold = var.db_allocated_storage_gb * 0.1 * 1073741824, periods = 1, desc = "RDS free storage below 10% of allocated" }
    memory      = { metric = "FreeableMemory", stat = "Minimum", op = "LessThanThreshold", threshold = 1073741824, periods = 3, desc = "RDS freeable memory below 1 GiB" }
    connections = { metric = "DatabaseConnections", stat = "Maximum", op = "GreaterThanThreshold", threshold = 800, periods = 3, desc = "More than 800 DB connections (add RDS Proxy?)" }
  }
  redis_alarms = {
    cpu    = { metric = "EngineCPUUtilization", threshold = 80, desc = "Redis engine CPU above 80%" }
    memory = { metric = "DatabaseMemoryUsagePercentage", threshold = 80, desc = "Redis memory above 80%" }
  }
}

resource "aws_cloudwatch_metric_alarm" "rds" {
  for_each            = local.rds_alarms
  alarm_name          = "${var.name}-rds-${each.key}"
  alarm_description   = each.value.desc
  namespace           = "AWS/RDS"
  metric_name         = each.value.metric
  dimensions          = { DBInstanceIdentifier = aws_db_instance.this.identifier }
  statistic           = each.value.stat
  period              = 300
  evaluation_periods  = each.value.periods
  threshold           = each.value.threshold
  comparison_operator = each.value.op
  alarm_actions       = local.alarm_actions
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "redis" {
  for_each            = local.redis_alarms
  alarm_name          = "${var.name}-redis-${each.key}"
  alarm_description   = each.value.desc
  namespace           = "AWS/ElastiCache"
  metric_name         = each.value.metric
  dimensions          = { ReplicationGroupId = aws_elasticache_replication_group.this.id }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 3
  threshold           = each.value.threshold
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = local.alarm_actions
  tags                = var.tags
}

# Backup failures, failovers and storage events page directly (operations §6 "Backups").
resource "aws_db_event_subscription" "rds" {
  name             = "${var.name}-rds-events"
  sns_topic        = aws_sns_topic.alarms.arn
  source_type      = "db-instance"
  source_ids       = [aws_db_instance.this.identifier]
  event_categories = ["backup", "failure", "failover", "low storage", "recovery", "restoration"]
  tags             = var.tags
}
