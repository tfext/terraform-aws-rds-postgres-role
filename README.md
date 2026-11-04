# terraform-aws-rds-postgres-role

Declares one PostgreSQL role on an RDS instance created by
[`terraform-aws-rds`](https://github.com/tfext/terraform-aws-rds): the tables it
reads and the tables it writes, or, for the one role that owns the schema, the
right to create tables and the extensions it needs. The role signs in with IAM
by default, and the module creates the `rds-db:connect` policy for it.

The work is done by a bundled script (`role.py`) run from `local-exec`
provisioners. It connects as the root user, reading the root password from the
RDS-managed secret (`master_user_secret_arn`) with the AWS CLI at run time, so
the password never enters the Terraform state.

```hcl
module "schema_role" {
  source       = "github.com/tfext/terraform-aws-rds-postgres-role?ref=<tag>"
  name         = "app-schema"
  schema_owner = true
  extensions   = ["postgis", "pg_trgm"]
  database     = local.database
}

module "app_role" {
  source   = "github.com/tfext/terraform-aws-rds-postgres-role?ref=<tag>"
  name     = "app"
  read     = ["counties"]
  write    = ["properties", "property_notes"]
  database = local.database
}

module "reporting_role" {
  source   = "github.com/tfext/terraform-aws-rds-postgres-role?ref=<tag>"
  name     = "reporting"
  read     = ["*"]
  owned_by = module.schema_role.name
  database = local.database
}

locals {
  database = {
    identifier        = "app-db"
    host              = module.db.host
    port              = module.db.port
    name              = module.db.name
    resource_id       = module.db.resource_id
    master_secret_arn = module.db.master_user_secret_arn
  }
}
```

Requirements
------------

Where Terraform runs needs `python3`, `psql` (the database's major version or
newer) and the AWS CLI, with credentials that can read the master secret (the
CLI uses the environment's credentials, such as `AWS_PROFILE`, not the
provider's). Reaching a private database through an SSM tunnel (`connection`)
also needs the Session Manager plugin, with the tunnel open while Terraform
runs, including on destroy.

The database must have been created with `manage_master_password = true`.

Inputs
------

| Name           | Type           | Default | Description                                                                                                                          |
|----------------|----------------|---------|--------------------------------------------------------------------------------------------------------------------------------------|
| `name`         | `string`       | -       | The role's name. Lowercase letters, digits, `_` and `-`, starting with a letter, up to 63 characters                                 |
| `database`     | `object`       | -       | `{ identifier, host, port, name, resource_id, master_secret_arn }`: the instance's identifier and its outputs from `terraform-aws-rds` |
| `connection`   | `object`       | `null`  | `{ host, port }` where the script reaches the database, when it isn't `database.host`: for example the local end of an SSM tunnel     |
| `read`         | `list(string)` | `[]`    | Tables the role reads. `["*"]` means every table, now and later                                                                       |
| `write`        | `list(string)` | `[]`    | Tables the role reads and writes, with the sequences they own. `["*"]` means every table and sequence, now and later                  |
| `schema_owner` | `bool`         | `false` | Make this the role that owns the schema: `CREATE` on schema `public`, and no table grants                                            |
| `owned_by`     | `string`       | `null`  | The schema role whose new tables `"*"` grants cover. Required when `read` or `write` is `["*"]`                                       |
| `extensions`   | `list(string)` | `[]`    | Extensions created by the root user before anything else. Only allowed with `schema_owner`                                           |
| `iam`          | `bool`         | `true`  | Sign in with IAM: the role gets `rds_iam`, and the module creates the connect policy                                                 |

Outputs
-------

| Name                 | Description                                                                                                   |
|----------------------|---------------------------------------------------------------------------------------------------------------|
| `name`               | The role's name, available once the role and its grants exist                                                |
| `connect_policy_arn` | IAM policy allowing `rds-db:connect` as this role, to attach to whatever signs in as it. `null` when `iam` is `false` |

What it does
------------

On create, in one transaction:

1. Creates any `extensions` (`CREATE EXTENSION IF NOT EXISTS`).
2. Creates the role as a login role if it doesn't exist, and grants it
   `rds_iam` when `iam` is on (revokes it when off).
3. For the schema owner: `GRANT CREATE, USAGE ON SCHEMA public`, and makes the
   root user a member of the role, so root can manage the role's tables.
4. For any other role: `CONNECT, TEMP` on the database and `USAGE` on schema
   `public`, then revokes all its table, sequence and default privileges and
   grants its lists:

   | List                  | Grants                                                                                              |
   |-----------------------|-----------------------------------------------------------------------------------------------------|
   | named table in `read`  | `SELECT`                                                                                            |
   | named table in `write` | `SELECT, INSERT, UPDATE, DELETE`, and `USAGE, SELECT` on the sequences the table owns               |
   | `read = ["*"]`         | `SELECT` on all tables, and the same as a default privilege for new tables of `owned_by`            |
   | `write = ["*"]`        | The write grants on all tables and sequences, and the same as default privileges for `owned_by`     |

   A named table that doesn't exist stops the apply with an error, before
   anything is changed.

Changing the role's grants, extensions or connection re-runs these steps
without dropping the role. Only a change of `name` or database replaces it.

On destroy, the role's privileges (including default privileges) are revoked
and the role is dropped. A schema owner that still owns tables can't be
dropped: PostgreSQL refuses, the error says why, and nothing is changed.

Set `DEBUG=1` in the environment to see the commands and SQL the script runs.
