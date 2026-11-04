#!/usr/bin/env python3
"""Manage an RDS PostgreSQL role and its table/sequence privileges."""

import json
import subprocess
import sys
import os


DEBUG = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")
SCHEMA = "public"

DEFAULT_ACL_TYPES = {
    "r": "TABLES",
    "S": "SEQUENCES",
    "f": "FUNCTIONS",
    "T": "TYPES",
    "n": "SCHEMAS",
    "L": "LARGE OBJECTS",
}


class Console:
    @staticmethod
    def trace(msg, *args):
        if DEBUG:
            print(msg, *args, file=sys.stderr)

    @staticmethod
    def info(msg, *args):
        print(msg, *args, file=sys.stderr)

    @staticmethod
    def error(msg, *args):
        Console.info(msg, *args)
        sys.exit(1)


class Postgres:
    @staticmethod
    def run(args, check=True, input=None, env=None):
        Console.trace("Running", *args)

        result = subprocess.run(args, capture_output=True, text=True, input=input, env=env)
        if check and result.returncode != 0:
            Console.error(f"{args[0]} error:\n{result.stderr}")

        Console.trace(result.stdout)

        return result

    @staticmethod
    def root_credentials(region, secret_arn):
        """Read the root user's name and password from the RDS-managed secret."""
        result = Postgres.run([
            "aws", "secretsmanager", "get-secret-value",
            "--region", region,
            "--secret-id", secret_arn,
            "--query", "SecretString",
            "--output", "text",
        ])
        try:
            secret = json.loads(result.stdout)
        except json.JSONDecodeError:
            Console.error(f"Secret {secret_arn} does not hold a JSON username/password")
        return secret["username"], secret["password"]

    @staticmethod
    def connect(config):
        """Build the environment psql runs with, connected as the root user."""
        database = config["database"]
        connection = config.get("connection") or {"host": database["host"], "port": database["port"]}
        user, password = Postgres.root_credentials(config["region"], database["master_secret_arn"])

        env = dict(os.environ)
        env.update({
            "PGHOST": connection["host"],
            "PGPORT": str(connection["port"]),
            "PGDATABASE": database["name"],
            "PGUSER": user,
            "PGPASSWORD": password,
            "PGSSLMODE": "require",
        })
        return env

    @staticmethod
    def psql(db, sql, single_transaction=False):
        args = ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]
        if single_transaction:
            args.append("-1")
        Console.trace(sql)
        return Postgres.run(args + ["-f", "-"], input=sql, env=db)


class Database:
    @staticmethod
    def exec(db, statements):
        """Run statements in one transaction, so a failure leaves nothing half-applied."""
        if statements:
            Postgres.psql(db, "\n".join(statements) + "\n", single_transaction=True)

    @staticmethod
    def list(db, sql):
        """Run a single-column SELECT and return its values as a list."""
        result = Postgres.psql(db, sql)
        return [line for line in result.stdout.splitlines() if line]

    @staticmethod
    def qi(name):
        return '"' + name.replace('"', '""') + '"'

    @staticmethod
    def ql(s):
        return "'" + s.replace("'", "''") + "'"

    @staticmethod
    def role_exists(db, name):
        return bool(Database.list(db, f"SELECT 1 FROM pg_roles WHERE rolname = {Database.ql(name)};"))

    @staticmethod
    def missing_tables(db, tables):
        if not tables:
            return []
        names = ", ".join(Database.ql(t) for t in tables)
        existing = set(Database.list(db, (
            f"SELECT c.relname FROM pg_class c"
            f" JOIN pg_namespace n ON c.relnamespace = n.oid"
            f" WHERE n.nspname = {Database.ql(SCHEMA)} AND c.relname IN ({names})"
            f" AND c.relkind IN ('r', 'p', 'v', 'm', 'f');"
        )))
        return [t for t in tables if t not in existing]

    @staticmethod
    def sequences_for_table(db, table):
        sql = (
            f"SELECT s.relname FROM pg_class s"
            f" JOIN pg_depend d ON d.objid = s.oid"
            f" JOIN pg_class t ON d.refobjid = t.oid"
            f" JOIN pg_namespace n ON t.relnamespace = n.oid"
            f" WHERE s.relkind = 'S' AND t.relname = {Database.ql(table)}"
            f" AND n.nspname = {Database.ql(SCHEMA)} AND d.deptype = 'a';"
        )
        return Database.list(db, sql)

    @staticmethod
    def revoke_default_privileges(db, name):
        """Statements removing every default privilege granted to the role, by any owner."""
        rows = Database.list(db, (
            f"SELECT DISTINCT d.defaclrole::regrole::text || '|' || coalesce(n.nspname, '') || '|' || d.defaclobjtype"
            f" FROM pg_default_acl d"
            f" LEFT JOIN pg_namespace n ON d.defaclnamespace = n.oid"
            f" CROSS JOIN LATERAL aclexplode(d.defaclacl) a"
            f" WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = {Database.ql(name)});"
        ))
        role = Database.qi(name)
        statements = []
        for row in rows:
            owner, schema, objtype = row.split("|")
            in_schema = f" IN SCHEMA {Database.qi(schema)}" if schema else ""
            # regrole::text is already quoted where needed
            statements.append(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner}{in_schema}"
                f" REVOKE ALL ON {DEFAULT_ACL_TYPES[objtype]} FROM {role};"
            )
        return statements

    @staticmethod
    def revoke_all(db, name):
        role = Database.qi(name)
        schema = Database.qi(SCHEMA)
        return Database.revoke_default_privileges(db, name) + [
            f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {schema} FROM {role};",
            f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {schema} FROM {role};",
        ]

    @staticmethod
    def grant_tables(db, name, database_name, read, write, owned_by):
        role = Database.qi(name)
        schema = Database.qi(SCHEMA)

        named_write = [t for t in write if t != "*"]
        named_read = [t for t in read if t != "*" and t not in named_write]

        missing = Database.missing_tables(db, named_read + named_write)
        if missing:
            Console.error(f"Tables not found in schema {SCHEMA}: {', '.join(missing)}")

        if ("*" in read or "*" in write) and not Database.role_exists(db, owned_by):
            Console.error(f"owned_by role '{owned_by}' does not exist")

        statements = [
            f"GRANT CONNECT, TEMP ON DATABASE {Database.qi(database_name)} TO {role};",
            f"GRANT USAGE ON SCHEMA {schema} TO {role};",
        ] + Database.revoke_all(db, name)

        if "*" in write:
            owner = Database.qi(owned_by)
            statements += [
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {role};",
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {role};",
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema}"
                f" GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role};",
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema}"
                f" GRANT USAGE, SELECT ON SEQUENCES TO {role};",
            ]
        elif "*" in read:
            owner = Database.qi(owned_by)
            statements += [
                f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role};",
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema}"
                f" GRANT SELECT ON TABLES TO {role};",
            ]

        for table in named_read:
            statements.append(f"GRANT SELECT ON TABLE {schema}.{Database.qi(table)} TO {role};")

        for table in named_write:
            t = f"{schema}.{Database.qi(table)}"
            statements.append(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {t} TO {role};")
            for seq in Database.sequences_for_table(db, table):
                statements.append(f"GRANT USAGE, SELECT ON SEQUENCE {schema}.{Database.qi(seq)} TO {role};")

        return statements


class Actions:
    @staticmethod
    def create(config):
        name = config["name"]
        role = Database.qi(name)
        db = Postgres.connect(config)

        statements = [f"CREATE EXTENSION IF NOT EXISTS {Database.qi(e)};" for e in config.get("extensions", [])]

        if Database.role_exists(db, name):
            statements.append(f"ALTER ROLE {role} LOGIN;")
        else:
            statements.append(f"CREATE ROLE {role} LOGIN;")

        if config.get("iam", True):
            statements.append(f"GRANT rds_iam TO {role};")
        else:
            statements.append(f"REVOKE rds_iam FROM {role};")

        if config.get("schema_owner", False):
            statements += [
                f"GRANT CREATE, USAGE ON SCHEMA {Database.qi(SCHEMA)} TO {role};",
                f"GRANT {role} TO CURRENT_USER;",
            ]
        else:
            # A role created by this run has no privileges yet, so the revokes find nothing to remove
            statements += Database.grant_tables(
                db, name, config["database"]["name"],
                config.get("read", []), config.get("write", []), config.get("owned_by"),
            )

        Database.exec(db, statements)
        Console.info(f"Created role '{name}'.")

    @staticmethod
    def destroy(config):
        name = config["name"]
        db = Postgres.connect(config)

        if Database.role_exists(db, name):
            role = Database.qi(name)
            Database.exec(db, Database.revoke_all(db, name) + [
                f"REVOKE ALL PRIVILEGES ON SCHEMA {Database.qi(SCHEMA)} FROM {role};",
                f"REVOKE ALL PRIVILEGES ON DATABASE {Database.qi(config['database']['name'])} FROM {role};",
                f"DROP ROLE {role};",
            ])
            Console.info(f"Destroyed role '{name}'.")
        else:
            Console.info(f"Role '{name}' does not exist. No action taken.")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("create", "destroy"):
        Console.error("Usage: role.py <create|destroy>")

    action = sys.argv[1]

    try:
        config = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        Console.error(f"Invalid JSON on stdin: {e}")

    for key in ("name", "region", "database"):
        assert config.get(key) is not None, f"Missing '{key}' in input JSON"
    for key in ("identifier", "host", "port", "name", "master_secret_arn"):
        assert config["database"].get(key) is not None, f"Missing 'database.{key}' in input JSON"

    if action == "create":
        Actions.create(config)
    elif action == "destroy":
        Actions.destroy(config)


if __name__ == "__main__":
    main()
