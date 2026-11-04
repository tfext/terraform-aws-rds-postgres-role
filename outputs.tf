output "name" {
  value       = var.name
  description = "The role's name"
  depends_on  = [terraform_data.grants]
}

output "connect_policy_arn" {
  value       = one(aws_iam_policy.connect[*].arn)
  description = "IAM policy allowing rds-db:connect as this role. Null when iam is false"
}
