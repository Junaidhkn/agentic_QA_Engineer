output "runner_endpoint_service_name" {
  description = "Pass to the execution plane as control_plane_endpoint_service"
  value       = aws_vpc_endpoint_service.runners.service_name
}
output "public_alb_dns_name" { value = aws_lb.public.dns_name }
output "hosts" { value = local.hosts }
output "cluster_name" { value = aws_ecs_cluster.this.name }
output "migrate_task_definition" { value = aws_ecs_task_definition.migrate.arn }
output "task_subnets" { value = var.private_subnet_ids }
output "task_security_group_id" { value = aws_security_group.tasks.id }
output "db_endpoint" { value = aws_db_instance.this.address }
output "db_master_secret_arn" { value = aws_db_instance.this.master_user_secret[0].secret_arn }
output "redis_endpoint" { value = aws_elasticache_replication_group.this.primary_endpoint_address }
output "artifacts_bucket" { value = aws_s3_bucket.artifacts.id }
output "audit_anchor_bucket" { value = aws_s3_bucket.audit.id }
output "lgtm_bucket" { value = aws_s3_bucket.lgtm.id }
output "kms_key_arn" { value = aws_kms_key.data.arn }
output "alarm_topic_arn" { value = aws_sns_topic.alarms.arn }
output "app_secret_arns" { value = { for k, s in aws_secretsmanager_secret.app : k => s.arn } }
