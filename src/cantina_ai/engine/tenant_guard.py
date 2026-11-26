"""Database-level tenant isolation for model-written SQL: one Postgres role per tenant.

When the model writes the SQL, isolation cannot depend on it remembering
`WHERE tenant_id = ...`. The database enforces it:

- a role per tenant, read-only, with a statement timeout;
- SELECT on an allow-list of tables and COLUMNS only (secrets never reachable);
- row-level security on every allowed table, with the tenant id WRITTEN INTO the
  policy (not read from a setting that the SQL could change with set_config);
- tables without the tenant column are scoped through a parent row (an EXISTS on
  the parent, which is itself filtered by its own policy).

Optionally, a second role (e.g. for hand-written tools) gets a permissive policy, so
enabling RLS does not hide rows from it. Table owners are not subject to RLS, so the
application that owns the tables keeps working unchanged.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TenantPolicy:
    database: str
    tenant_table: str  # the table whose rows ARE the tenants (filtered by its id)
    tenant_column: str  # the column that points at the tenant in the other tables
    columns: dict[str, list[str]]  # table -> columns the model may read
    parents: dict[str, str] = field(default_factory=dict)  # table -> USING (...) condition
    role_prefix: str = "ai_tenant"
    statement_timeout: str = "5s"
    see_all_role: str | None = None  # a role that keeps seeing every row


def role_for(policy: TenantPolicy, tenant_id: int) -> str:
    return f"{policy.role_prefix}_{int(tenant_id)}"


def setup_sql(policy: TenantPolicy, tenant_id: int) -> str:
    role, t = role_for(policy, tenant_id), int(tenant_id)
    out = [
        "BEGIN;",
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
        f"THEN CREATE ROLE {role} LOGIN; END IF; END $$;",
        f"ALTER ROLE {role} SET default_transaction_read_only = on;",
        f"ALTER ROLE {role} SET statement_timeout = '{policy.statement_timeout}';",
        f"GRANT CONNECT ON DATABASE {policy.database} TO {role};",
        f"GRANT USAGE ON SCHEMA public TO {role};",
    ]
    for table, cols in policy.columns.items():
        if table == policy.tenant_table:
            cond = f"id = {t}"
        else:
            cond = policy.parents.get(table, f"{policy.tenant_column} = {t}")
        out += [
            f"GRANT SELECT ({', '.join(cols)}) ON {table} TO {role};",
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;",
            f"DROP POLICY IF EXISTS {role} ON {table};",
            f"CREATE POLICY {role} ON {table} FOR SELECT TO {role} USING ({cond});",
        ]
        if policy.see_all_role:
            out += [
                f"DROP POLICY IF EXISTS see_all ON {table};",
                f"CREATE POLICY see_all ON {table} FOR SELECT TO {policy.see_all_role} "
                "USING (true);",
            ]
    out.append("COMMIT;")
    return "\n".join(out)


def teardown_sql(policy: TenantPolicy, tenant_id: int) -> str:
    role = role_for(policy, tenant_id)
    out = ["BEGIN;"]
    for table in policy.columns:
        out += [f"DROP POLICY IF EXISTS {role} ON {table};", f"REVOKE ALL ON {table} FROM {role};"]
    out += [
        f"REVOKE ALL ON SCHEMA public FROM {role};",
        f"REVOKE ALL ON DATABASE {policy.database} FROM {role};",
        f"DROP ROLE IF EXISTS {role};",
        "COMMIT;",
    ]
    return "\n".join(out)
