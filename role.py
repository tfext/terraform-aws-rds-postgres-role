#!/usr/bin/env python3
"""Manage an RDS PostgreSQL role and its table/sequence privileges."""

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time


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


def run(args, check=True, input=None, env=None):
    Console.trace("Running", *args)

    result = subprocess.run(args, capture_output=True, text=True, input=input, env=env)
    if check and result.returncode != 0:
        Console.error(f"{args[0]} error:\n{result.stderr}")

    Console.trace(result.stdout)

    return result


class Tools:
    README = "https://github.com/tfext/terraform-aws-rds-postgres-role#requirements"

    @staticmethod
    def check(config):
        """Stop before anything is changed if a tool the run needs isn't on the PATH."""
        needed = ["aws", "psql"]
        if config.get("tunnel"):
            needed.append("session-manager-plugin")
        missing = [tool for tool in needed if not shutil.which(tool)]
        if missing:
            Console.error(f"Missing {', '.join(missing)} where Terraform runs. See {Tools.README}")


class Aws:
    @staticmethod
    def environment(config):
        """The environment AWS CLI calls run with: the configured profile, then the assumed role, if any."""
        aws = config.get("aws") or {}
        env = dict(os.environ)
        if aws.get("profile"):
            env["AWS_PROFILE"] = aws["profile"]
        if aws.get("role_arn"):
            result = run([
                "aws", "sts", "assume-role",
                "--role-arn", aws["role_arn"],
                "--role-session-name", f"rds-postgres-role-{config['name']}",
                "--query", "Credentials",
                "--output", "json",
            ], env=env)
            credentials = json.loads(result.stdout)
            env.pop("AWS_PROFILE", None)
            env.update({
                "AWS_ACCESS_KEY_ID": credentials["AccessKeyId"],
                "AWS_SECRET_ACCESS_KEY": credentials["SecretAccessKey"],
                "AWS_SESSION_TOKEN": credentials["SessionToken"],
            })
        env["AWS_REGION"] = config["region"]
        return env


class Tunnel:
    READY_TIMEOUT = 60

    @staticmethod
    def instance(config, aws_env):
        """An in-service instance of the tunnel's autoscaling group, looked up on every run."""
        group = config["tunnel"]["autoscaling_group"]
        result = run([
            "aws", "autoscaling", "describe-auto-scaling-groups",
            "--auto-scaling-group-names", group,
            "--query", "AutoScalingGroups[0].Instances[?LifecycleState=='InService'].InstanceId",
            "--output", "json",
        ], env=aws_env)
        instances = json.loads(result.stdout) or []
        if not instances:
            Console.error(f"Autoscaling group {group} has no in-service instance to tunnel through")
        return instances[0]

    @staticmethod
    def free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @staticmethod
    @contextlib.contextmanager
    def open(config, aws_env):
        """Forward a local port to the database through an SSM session, for the duration of the block."""
        if not config.get("tunnel"):
            yield None
            return

        database = config["database"]
        instance = Tunnel.instance(config, aws_env)
        port = Tunnel.free_port()
        args = [
            "aws", "ssm", "start-session",
            "--target", instance,
            "--document-name", "AWS-StartPortForwardingSessionToRemoteHost",
            "--parameters", json.dumps({
                "host": [database["host"]],
                "portNumber": [str(database["port"])],
                "localPortNumber": [str(port)],
            }),
        ]
        Console.trace("Running", *args)
        session = subprocess.Popen(args, env=aws_env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.monotonic() + Tunnel.READY_TIMEOUT
            while True:
                if session.poll() is not None:
                    Console.error(f"The SSM session through {instance} ended:\n{session.stdout.read()}")
                with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
                if time.monotonic() > deadline:
                    Console.error(f"The SSM session through {instance} didn't open port {port} within {Tunnel.READY_TIMEOUT}s")
                time.sleep(0.5)
            Console.trace(f"Tunnel open through {instance} on port {port}")
            yield {"host": "127.0.0.1", "port": port}
        finally:
            session.terminate()
            try:
                session.wait(timeout=10)
            except subprocess.TimeoutExpired:
                session.kill()


class Postgres:
    @staticmethod
    def run(args, check=True, input=None, env=None):
        return run(args, check=check, input=input, env=env)

    @staticmethod
    def root_credentials(aws_env, secret_arn):
        """Read the root user's name and password from the RDS-managed secret."""
        result = Postgres.run([
            "aws", "secretsmanager", "get-secret-value",
            "--secret-id", secret_arn,
            "--query", "SecretString",
            "--output", "text",
        ], env=aws_env)
        try:
            secret = json.loads(result.stdout)
        except json.JSONDecodeError:
            Console.error(f"Secret {secret_arn} does not hold a JSON username/password")
        return secret["username"], secret["password"]

    @staticmethod
    def iam_token(aws_env, database, user):
        """An IAM authentication token for signing in as user, generated for the database's real endpoint."""
        return Postgres.run([
            "aws", "rds", "generate-db-auth-token",
            "--hostname", database["host"],
            "--port", str(database["port"]),
            "--username", user,
        ], env=aws_env).stdout.strip()

    @staticmethod
    @contextlib.contextmanager
    def connect(config):
        """Yield the connections psql runs with, through the tunnel if there is one."""
        database = config["database"]
        aws_env = Aws.environment(config)
        user, password = Postgres.root_credentials(aws_env, database["master_secret_arn"])

        with Tunnel.open(config, aws_env) as tunnel:
            def env(user, password):
                env = dict(os.environ)
                env.update({
                    "PGHOST": database["host"],
                    "PGPORT": str(database["port"]),
                    "PGDATABASE": database["name"],
                    "PGUSER": user,
                    "PGPASSWORD": password,
                    "PGSSLMODE": "require",
                })
                if tunnel:
                    # The hostname stays the database's, so TLS is checked against it
                    env.update({"PGHOSTADDR": tunnel["host"], "PGPORT": str(tunnel["port"])})
                return env

            yield Connections(
                root=env(user, password),
                signed_in_as=lambda role: env(role, Postgres.iam_token(aws_env, database, role)),
            )

    @staticmethod
    def psql(db, sql, single_transaction=False):
        args = ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]
        if single_transaction:
            args.append("-1")
        Console.trace(sql)
        return Postgres.run(args + ["-f", "-"], input=sql, env=db)


class Connections:
    """The root user's connection, for roles, extensions and schema and database privileges, and a
    way to sign in as another role with IAM: the schema owner, for privileges on its tables. Root
    can't be a member of the schema owner, since RDS makes any member of rds_iam, however
    indirect, sign in with IAM only."""

    def __init__(self, root, signed_in_as):
        self.root = root
        self.signed_in_as = signed_in_as


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
    def is_member(db, member, name):
        """Whether member (a quoted role name, or CURRENT_USER) is directly a member of the role."""
        return bool(Database.list(db, (
            f"SELECT 1 FROM pg_auth_members"
            f" WHERE member = (SELECT oid FROM pg_roles WHERE rolname = {member})"
            f" AND roleid = (SELECT oid FROM pg_roles WHERE rolname = {Database.ql(name)});"
        )))

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
    def revoke_default_privileges(db, name, owned_by):
        """Statements removing every default privilege owned_by granted to the role. Run as owned_by."""
        rows = Database.list(db, (
            f"SELECT DISTINCT coalesce(n.nspname, '') || '|' || d.defaclobjtype::text"
            f" FROM pg_default_acl d"
            f" LEFT JOIN pg_namespace n ON d.defaclnamespace = n.oid"
            f" CROSS JOIN LATERAL aclexplode(d.defaclacl) a"
            f" WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = {Database.ql(name)})"
            f" AND d.defaclrole = (SELECT oid FROM pg_roles WHERE rolname = {Database.ql(owned_by)});"
        ))
        role = Database.qi(name)
        owner = Database.qi(owned_by)
        statements = []
        for row in rows:
            schema, objtype = row.split("|")
            in_schema = f" IN SCHEMA {Database.qi(schema)}" if schema else ""
            statements.append(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner}{in_schema}"
                f" REVOKE ALL ON {DEFAULT_ACL_TYPES[objtype]} FROM {role};"
            )
        return statements

    @staticmethod
    def revoke_all(db, name, owned_by):
        """Statements removing the role's privileges on owned_by's tables and sequences. Run as owned_by."""
        role = Database.qi(name)
        schema = Database.qi(SCHEMA)
        return Database.revoke_default_privileges(db, name, owned_by) + [
            f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {schema} FROM {role};",
            f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {schema} FROM {role};",
        ]

    @staticmethod
    def grant_tables(db, name, read, write, owned_by):
        """Statements replacing the role's privileges on owned_by's tables with its lists. Run as owned_by."""
        role = Database.qi(name)
        schema = Database.qi(SCHEMA)

        named_write = [t for t in write if t != "*"]
        named_read = [t for t in read if t != "*" and t not in named_write]

        statements = Database.revoke_all(db, name, owned_by)

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
    def check(config, root):
        """Stop before anything is changed if the role's grants can't be made."""
        read, write, owned_by = config.get("read", []), config.get("write", []), config.get("owned_by")
        if not (read or write):
            return
        if not Database.role_exists(root, owned_by):
            Console.error(f"owned_by role '{owned_by}' does not exist")
        missing = Database.missing_tables(root, [t for t in read + write if t != "*"])
        if missing:
            Console.error(f"Tables not found in schema {SCHEMA}: {', '.join(missing)}")

    @staticmethod
    def create(config, connections):
        name = config["name"]
        role = Database.qi(name)
        root = connections.root
        Actions.check(config, root)

        statements = [f"CREATE EXTENSION IF NOT EXISTS {Database.qi(e)};" for e in config.get("extensions", [])]

        if Database.role_exists(root, name):
            statements.append(f"ALTER ROLE {role} LOGIN;")
        else:
            statements.append(f"CREATE ROLE {role} LOGIN;")

        if config.get("iam", True):
            statements.append(f"GRANT rds_iam TO {role};")
        else:
            statements.append(f"REVOKE rds_iam FROM {role};")

        if config.get("schema_owner", False):
            statements.append(f"GRANT CREATE, USAGE ON SCHEMA {Database.qi(SCHEMA)} TO {role};")
            # Root must not be a member: RDS would make it sign in with IAM only
            if Database.is_member(root, "CURRENT_USER", name):
                statements.append(f"REVOKE {role} FROM CURRENT_USER;")
            Database.exec(root, statements)
        else:
            Database.exec(root, statements + [
                f"GRANT CONNECT, TEMP ON DATABASE {Database.qi(config['database']['name'])} TO {role};",
                f"GRANT USAGE ON SCHEMA {Database.qi(SCHEMA)} TO {role};",
            ])
            read, write, owned_by = config.get("read", []), config.get("write", []), config.get("owned_by")
            if read or write:
                owner = connections.signed_in_as(owned_by)
                # A role created by this run has no privileges yet, so the revokes find nothing to remove
                Database.exec(owner, Database.grant_tables(owner, name, read, write, owned_by))

        Console.info(f"Created role '{name}'.")

    @staticmethod
    def destroy(config, connections):
        name = config["name"]
        root = connections.root

        if not Database.role_exists(root, name):
            Console.info(f"Role '{name}' does not exist. No action taken.")
            return

        owned_by = config.get("owned_by")
        if owned_by and Database.role_exists(root, owned_by):
            owner = connections.signed_in_as(owned_by)
            Database.exec(owner, Database.revoke_all(owner, name, owned_by))

        role = Database.qi(name)
        Database.exec(root, [
            f"REVOKE ALL PRIVILEGES ON SCHEMA {Database.qi(SCHEMA)} FROM {role};",
            f"REVOKE ALL PRIVILEGES ON DATABASE {Database.qi(config['database']['name'])} FROM {role};",
            f"DROP ROLE {role};",
        ])
        Console.info(f"Destroyed role '{name}'.")


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

    Tools.check(config)

    with Postgres.connect(config) as connections:
        if action == "create":
            Actions.create(config, connections)
        elif action == "destroy":
            Actions.destroy(config, connections)


if __name__ == "__main__":
    main()
