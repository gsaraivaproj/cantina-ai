"""Text-to-SQL: the model reads the schema, writes a SELECT, the database runs it.

Safety lives in the database (tenant_guard.py: a role per tenant, RLS, a column
allow-list, read-only, timeout). This module only adds what the database cannot:
one statement, SELECT/WITH only, a row cap, and readable errors so the model can fix
its own query.

The schema comes from information_schema as seen by the tenant's role, so it lists
exactly the columns the model may use. The `notes` are the "semantic layer": what
the columns mean and the traps of the schema; pass None to measure their effect.
"""

import re
from decimal import Decimal
from typing import Any, LiteralString, cast

import psycopg

MAX_ROWS = 200
MAX_CELL = 120
_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


class SQLInputError(ValueError):
    """A query the tool refuses, or a database error, explained to the model."""


def describe(conn: psycopg.Connection[Any], notes: str | None, today: str) -> str:
    rows = conn.execute(
        """
        SELECT table_name, string_agg(column_name || ' ' || data_type, ', '
                                      ORDER BY ordinal_position)
        FROM information_schema.columns
        WHERE table_schema = 'public'
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    schema = "\n".join(f"- {t}({cols})" for t, cols in rows)
    head = f"Tables and columns you can query (Postgres; today is {today}):"
    return f"{head}\n{schema}" + (f"\n\n{notes}" if notes else "")


def _cell(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        v = Decimal(str(v))
    s = str(v)
    return s if len(s) <= MAX_CELL else s[: MAX_CELL - 1] + "…"


def run_select(conn: psycopg.Connection[Any], sql: str, max_rows: int = MAX_ROWS) -> str:
    query = sql.strip().rstrip(";").strip()
    if not _START.match(query):
        raise SQLInputError("only SELECT queries (or WITH ... SELECT)")
    if ";" in query:
        raise SQLInputError("one statement at a time (no ';' in the middle)")
    wrapped = f"SELECT * FROM (\n{query}\n) AS q LIMIT {max_rows + 1}"
    try:
        # Model-written SQL on purpose: the role, RLS and grants are the guard.
        cur = conn.execute(cast(LiteralString, wrapped))
        rows = cur.fetchall()
    except psycopg.Error as exc:
        conn.rollback()
        message = (exc.diag.message_primary or str(exc)).strip()
        hint = f" (hint: {exc.diag.message_hint})" if exc.diag.message_hint else ""
        raise SQLInputError(f"Postgres error: {message}{hint}") from exc
    names = [d.name for d in cur.description or []]
    cut = len(rows) > max_rows
    rows = rows[:max_rows]
    lines = [f"{len(rows)} row(s)" + (f" (cut at {max_rows}: aggregate more)" if cut else "")]
    lines.append(" | ".join(names))
    lines += [" | ".join(_cell(v) for v in r) for r in rows]
    return "\n".join(lines)
