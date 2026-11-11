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

variable "tunnel" {
  type = object({
    autoscaling_group = string
  })
  default     = null
  description = "Reach a private database through an SSM port-forwarding session, opened by the script for each run through an in-service instance of this autoscaling group"
}

variable "aws" {
  type = object({
    profile  = optional(string)
    role_arn = optional(string)
  })
  default     = {}
  nullable    = false
  description = "Credentials for the script's AWS CLI calls (the master secret, and the tunnel): a profile, then a role assumed from it. Usually the same as the provider's. Unset means the environment's credentials"
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
  description = "The schema role that owns the tables in read and write. The grants are made signed in as it, with IAM. Required when read or write is set"

  validation {
    condition     = var.owned_by != null || (length(var.read) == 0 && length(var.write) == 0)
    error_message = "owned_by is required when read or write is set."
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
