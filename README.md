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
  owned_by = module.schema_role.name
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
newer) and the AWS CLI. The CLI can't use the provider's credentials, so give
it the same ones with `aws`: the provider's profile and the role it assumes.
Left unset, it uses the environment's credentials, such as `AWS_PROFILE`. They
need to read the master secret, to sign in to the database as `owned_by`
(`rds-db:connect`) and, with a `tunnel`, to describe the autoscaling group and
start SSM sessions on its instances.

A `tunnel` also needs the Session Manager plugin (`session-manager-plugin`) on
the `PATH`.

| Tool                     | Install                                                                                                                                                                                                                  |
|--------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| AWS CLI v2               | [Installing the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)                                                                                                                 |
| `psql`                   | Debian/Ubuntu: `apt install postgresql-client-<major>` (from the [PostgreSQL apt repository](https://www.postgresql.org/download/linux/ubuntu/)); macOS: `brew install libpq`                                         |
| `session-manager-plugin` | Debian/Ubuntu: `curl -sSo /tmp/smp.deb https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb && sudo dpkg -i /tmp/smp.deb`; macOS: `brew install --cask session-manager-plugin`; others: [Installing the plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) |

The script checks for these before changing anything, and stops with an error
naming whichever are missing.

The database must have been created with `manage_master_password = true`.

Inputs
------

| Name           | Type           | Default | Description                                                                                                                          |
|----------------|----------------|---------|--------------------------------------------------------------------------------------------------------------------------------------|
| `name`         | `string`       | -       | The role's name. Lowercase letters, digits, `_` and `-`, starting with a letter, up to 63 characters                                 |
| `database`     | `object`       | -       | `{ identifier, host, port, name, resource_id, master_secret_arn }`: the instance's identifier and its outputs from `terraform-aws-rds` |
| `tunnel`       | `object`       | `null`  | `{ autoscaling_group }`: reach a private database through an SSM port-forwarding session on an in-service instance of this group     |
| `aws`          | `object`       | `{}`    | `{ profile, role_arn }`, both optional: the credentials for the script's AWS CLI calls, a profile and then a role assumed from it   |
| `read`         | `list(string)` | `[]`    | Tables the role reads. `["*"]` means every table, now and later                                                                       |
| `write`        | `list(string)` | `[]`    | Tables the role reads and writes, with the sequences they own. `["*"]` means every table and sequence, now and later                  |
| `schema_owner` | `bool`         | `false` | Make this the role that owns the schema: `CREATE` on schema `public`, and no table grants                                            |
| `owned_by`     | `string`       | `null`  | The schema role that owns the tables in `read` and `write`. The grants are made signed in as it, with IAM. Required when either is set |
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

On create:

1. Creates any `extensions` (`CREATE EXTENSION IF NOT EXISTS`).
2. Creates the role as a login role if it doesn't exist, and grants it
   `rds_iam` when `iam` is on (revokes it when off).
3. For the schema owner: `GRANT CREATE, USAGE ON SCHEMA public`. Root is never
   made a member of it: RDS makes any member of `rds_iam`, however indirect,
   sign in with IAM only, which would stop root's password working. Anyone
   managing the schema owner's tables signs in as it, with IAM.
4. For any other role: `CONNECT, TEMP` on the database and `USAGE` on schema
   `public`. Then, signed in as `owned_by` with an IAM token, it revokes all
   the role's privileges on `owned_by`'s tables and sequences, and its
   default privileges from `owned_by`, and grants its lists:

   | List                  | Grants                                                                                              |
   |-----------------------|-----------------------------------------------------------------------------------------------------|
   | named table in `read`  | `SELECT`                                                                                            |
   | named table in `write` | `SELECT, INSERT, UPDATE, DELETE`, and `USAGE, SELECT` on the sequences the table owns               |
   | `read = ["*"]`         | `SELECT` on all tables, and the same as a default privilege for new tables of `owned_by`            |
   | `write = ["*"]`        | The write grants on all tables and sequences, and the same as default privileges for `owned_by`     |

   A named table, or an `owned_by` role, that doesn't exist stops the apply
   with an error, before anything is changed.

Root's statements run in one transaction, and the owner's in another. Every
step is idempotent, so a run that fails between them is completed by the next.

Changing the role's grants or extensions re-runs these steps without dropping
the role. Changing `tunnel` or `aws` doesn't re-run them. Only a change of
`name` or database replaces it.

On destroy, the role's privileges on `owned_by`'s tables (including default
privileges) are revoked as `owned_by`, then root revokes the rest and drops the
role. A schema owner that still owns tables can't be
dropped: PostgreSQL refuses, the error says why, and nothing is changed.

Private databases
-----------------

With a `tunnel`, each run of the script (create and destroy alike) finds an
in-service instance of the group, starts an
`AWS-StartPortForwardingSessionToRemoteHost` session from a free local port to
`database.host`, and ends it when the script finishes. psql connects to the
local port with `PGHOSTADDR`, keeping `PGHOST` as the database's hostname, so
TLS is checked against the real name. The instance is looked up on every run,
so a destroy still works after the group's instances have been replaced.

The instances need the SSM agent and `AmazonSSMManagedInstanceCore`, and the
database's security group must allow them, as for the hosts of an
`aws-ecs-cluster`:

```hcl
module "schema_role" {
  source       = "github.com/tfext/terraform-aws-rds-postgres-role?ref=<tag>"
  name         = "app-schema"
  schema_owner = true
  database     = local.database
  tunnel       = { autoscaling_group = "app_ecs_hosts" }
  aws          = { profile = "ops", role_arn = "arn:aws:iam::123456789012:role/terraform" }
}
```

The database's `host` isn't known until it exists, so a new database and its
roles take two applies: `-target` the database first.

Set `DEBUG=1` in the environment to see the commands and SQL the script runs.
