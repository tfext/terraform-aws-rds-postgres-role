data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  config = {
    name         = var.name
    region       = data.aws_region.current.region
    database     = var.database
    connection   = var.connection
    read         = var.read
    write        = var.write
    schema_owner = var.schema_owner
    owned_by     = var.owned_by
    extensions   = var.extensions
    iam          = var.iam
  }
  script_path = "${path.module}/role.py"
}

# The role itself. Replaced only when the role's identity changes, so a change
# to its grants or extensions never drops it. Destroying it revokes the role's
# privileges and drops it, using the script and configuration saved by the
# last apply.
resource "terraform_data" "role" {
  triggers_replace = {
    name     = var.name
    database = var.database.identifier
    db_name  = var.database.name
  }

  input = {
    script = file(local.script_path)
    config = jsonencode(local.config)
  }

  provisioner "local-exec" {
    when        = destroy
    command     = "printf '%s' \"$INPUT\" | python3 -c \"$SCRIPT_CONTENT\" destroy"
    environment = { SCRIPT_CONTENT = self.output.script, INPUT = self.output.config }
  }
}

# Creates the role if needed and applies its extensions and grants. Re-runs
# whenever the configuration changes; every step is idempotent.
resource "terraform_data" "grants" {
  triggers_replace = { config = jsonencode(local.config) }

  provisioner "local-exec" {
    command     = "printf '%s' \"$INPUT\" | python3 '${local.script_path}' create"
    environment = { INPUT = jsonencode(local.config) }
  }

  depends_on = [terraform_data.role]
}

resource "aws_iam_policy" "connect" {
  count = var.iam ? 1 : 0
  name  = "rds-connect-${var.database.identifier}-${var.name}"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "rds-db:connect"
      Resource = "arn:aws:rds-db:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:dbuser:${var.database.resource_id}/${var.name}"
    }]
  })
}
