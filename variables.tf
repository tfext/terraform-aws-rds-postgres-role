variable "name" {
  type        = string
  nullable    = false
  description = "The role's name"

  validation {
    condition     = can(regex("^[a-z][a-z0-9_-]{0,62}$", var.name))
    error_message = "Name must start with a lowercase letter and contain only lowercase letters, digits, underscores and hyphens, up to 63 characters."
  }
}

variable "database" {
  type = object({
    identifier        = string
    host              = string
    port              = number
    name              = string
    resource_id       = string
    master_secret_arn = string
  })
  nullable    = false
  description = "The database instance (identifier, such as ppta-stg) and its outputs from the terraform-aws-rds module"
}

variable "connection" {
  type        = object({ host = string, port = number })
  default     = null
  description = "Where the script reaches the database, when it isn't database.host: for example the local end of an SSM tunnel"
}

variable "read" {
  type        = list(string)
  default     = []
  nullable    = false
  description = "Tables the role reads. [\"*\"] means every table, now and later"

  validation {
    condition     = !contains(var.read, "*") || length(var.read) == 1
    error_message = "\"*\" must be the only entry in read."
  }
}

variable "write" {
  type        = list(string)
  default     = []
  nullable    = false
  description = "Tables the role reads and writes, with their sequences. [\"*\"] means every table, now and later"

  validation {
    condition     = !contains(var.write, "*") || length(var.write) == 1
    error_message = "\"*\" must be the only entry in write."
  }
}

variable "schema_owner" {
  type        = bool
  default     = false
  nullable    = false
  description = "Make this the role that owns the schema: CREATE on schema public, and no table grants"

  validation {
    condition     = !var.schema_owner || (length(var.read) == 0 && length(var.write) == 0)
    error_message = "A schema_owner role has no read or write grants."
  }
}

variable "owned_by" {
  type        = string
  default     = null
  description = "The schema role whose new tables \"*\" grants cover. Required when read or write is [\"*\"]"

  validation {
    condition     = var.owned_by != null || !(contains(var.read, "*") || contains(var.write, "*"))
    error_message = "owned_by is required when read or write is [\"*\"]."
  }
}

variable "extensions" {
  type        = list(string)
  default     = []
  nullable    = false
  description = "Extensions created by the root user before anything else. Only allowed with schema_owner"

  validation {
    condition     = length(var.extensions) == 0 || var.schema_owner
    error_message = "extensions are only allowed with schema_owner = true."
  }
}

variable "iam" {
  type        = bool
  default     = true
  nullable    = false
  description = "Sign in with IAM: the role gets rds_iam, and the module creates the connect policy"
}
