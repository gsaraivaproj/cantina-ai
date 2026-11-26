"""MCP server (stdio) for the demo cafeteria.

    CANTINA_SCHOOL_ID=1 uv run cantina-mcp

The school is configuration, never a tool argument. Every tool connects as that
school's database role (row-level security), so a mistake in a query, or a model
asked to "ignore the configured school", still cannot read another school.

Environment:
- CANTINA_SCHOOL_ID   the school this server answers for (required)
- CANTINA_DB          base connection string (default "dbname=cantina_demo")
- CANTINA_AI_DB       traces and the docs index (default "dbname=cantina_ai")
- CANTINA_TOOLSET     "fixed" | "sql" | "fixed+sql" (default), for experiments
- CANTINA_SCHEMA_NOTES  "0" sends the schema without the semantic notes
- CANTINA_OLLAMA_URL  embeddings for search_rules (default http://localhost:11434)
"""

import asyncio
import os
import sys
from collections.abc import Callable
from typing import Any

import psycopg
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from cantina_ai.demo import tools
from cantina_ai.demo.access import NOTES, POLICY
from cantina_ai.engine import docs_index, text_to_sql
from cantina_ai.engine.embeddings import OllamaEmbedder
from cantina_ai.engine.tenant_guard import role_for
from cantina_ai.engine.tracing import Tracer

SCHOOL = int(os.environ.get("CANTINA_SCHOOL_ID") or 0) or sys.exit("set CANTINA_SCHOOL_ID")
BASE_DB = os.environ.get("CANTINA_DB", "dbname=cantina_demo")
DB = f"{BASE_DB} user={role_for(POLICY, SCHOOL)}"
AI_DB = os.environ.get("CANTINA_AI_DB", "dbname=cantina_ai")
TOOLSET = os.environ.get("CANTINA_TOOLSET", "fixed+sql")
WITH_NOTES = os.environ.get("CANTINA_SCHEMA_NOTES", "1") != "0"

INSTRUCTIONS = (
    "Data of ONE school cafeteria: students, prepaid balances, sales, stock, cash registers, "
    "orders. Values come already computed by the database: do not redo the math. Prefer the "
    "specific tools; query_sql is the last resort for crossings they do not do. Use "
    "search_rules for WHY the system behaves as it does. Dates are YYYY-MM-DD, Brazil time."
)

server = MCPServer(name="cantina", title="Cantina AI demo", instructions=INSTRUCTIONS)
tracer = Tracer(AI_DB, SCHOOL)


def _call(tool: str, fn: Callable[..., str], **args: Any) -> str:
    def work() -> str:
        with psycopg.connect(DB) as conn:
            return fn(conn, SCHOOL, **args)

    try:
        return tracer.run(tool, work, args)
    except (tools.ToolInputError, text_to_sql.SQLInputError) as exc:
        # ToolError: the SDK shows its message to the model (other exceptions are generic).
        raise ToolError(str(exc)) from exc


def fixed(fn: Callable[..., str]) -> Callable[..., str]:
    return server.tool(structured_output=False)(fn) if "fixed" in TOOLSET else fn


@fixed
def find_student(name: str) -> str:
    """Find students of this school by name or part of it (accents optional). Returns id,
    class, balance, daily limit and guardians. Use the id in the other tools; if more than
    one comes back, ask the user which one."""
    return _call("find_student", tools.find_student, name=name)


@fixed
def student_statement(student_id: int, days: int = 30) -> str:
    """A student's statement for the last `days` (default 30, max 120): top-ups, purchases,
    lunches, refunds, with the balance after each one, the line where it went negative (if
    it did) and a summary. Use it to explain why a balance is what it is."""
    return _call("student_statement", tools.student_statement, student_id=student_id, days=days)


@fixed
def students_by_balance(below: float = 0.0, limit: int = 50) -> str:
    """Students whose balance is below `below` (default 0: the negative ones), lowest first,
    with how many, the total, the guardian and the last top-up. For "who owes?", "how many
    are negative?", "who needs a top-up?" (e.g. below=5)."""
    return _call("students_by_balance", tools.students_by_balance, below=below, limit=limit)


@fixed
def sales(start: str | None = None, end: str | None = None, group_by: str | None = None) -> str:
    """Revenue in a period: total, number of sales and items, excluding cancelled sales and
    top-ups (a top-up is not a sale). `start`/`end` as YYYY-MM-DD (none: today; only start:
    that day). `group_by` (optional): "day", "product", "category" or "sale_type" (student
    balance, counter sale without a student, lunch)."""
    return _call("sales", tools.sales, start=start, end=end, group_by=group_by)


@fixed
def low_stock(cover_days: int = 5) -> str:
    """Products running out: those that, at the pace of the last 14 days (including inside
    combos), last fewer than `cover_days` school days (default 5), and the sold-out ones."""
    return _call("low_stock", tools.low_stock, cover_days=cover_days)


@fixed
def cash_registers(day: str | None = None) -> str:
    """Cash registers: every one OPEN now and those closed on `day` (YYYY-MM-DD, default
    today), with what came in and went out (PIX/cash top-ups, counter sales, withdrawals,
    the end-of-day close) and whether the balance matches its movements. For "did the
    registers close right?" and forgotten open registers."""
    return _call("cash_registers", tools.cash_registers, day=day)


@fixed
def pending_orders() -> str:
    """Paid orders not handed over yet at the counter (two-step flow), oldest first, with
    who bought, the items and how long they have been waiting."""
    return _call("pending_orders", tools.pending_orders)


if "sql" in TOOLSET:

    @server.tool(structured_output=False)
    def describe_database() -> str:
        """Tables and columns query_sql can use, with notes on what the data means. Call it
        before writing SQL."""
        return _call(
            "describe_database",
            lambda conn, _s: text_to_sql.describe(
                conn, NOTES if WITH_NOTES else None, tools.today().isoformat()
            ),
        )

    @server.tool(structured_output=False)
    def query_sql(sql: str, reason: str) -> str:
        """LAST RESORT: run a read-only SELECT on the cafeteria database. First check whether a
        specific tool answers (sales, students, statement, stock, registers, orders); use this
        only for crossings they do not do. Call describe_database first. `reason`: why the
        other tools are not enough. Aggregate in SQL (200 rows max); on an error, fix the
        query and try again."""
        if not reason.strip():
            raise ToolError("explain in 'reason' why the other tools are not enough")
        return _call(
            "query_sql",
            lambda conn, _s, sql, reason: text_to_sql.run_select(conn, sql),
            sql=sql,
            reason=reason,
        )


@server.tool(structured_output=False)
async def search_rules(query: str) -> str:
    """Semantic search in the system's documentation (business rules, flows): the WHY behind
    the data, e.g. "why is lunch charged without balance?" or "how is a register closed?".
    Write the query as a sentence about the subject; cite the document and section."""
    if not query.strip() or len(query) > 500:
        raise ToolError("'query' must be a non-empty text (up to 500 characters)")
    embedder = OllamaEmbedder(
        os.environ.get("CANTINA_OLLAMA_URL", "http://localhost:11434"), "bge-m3", keep_alive="1m"
    )
    try:
        hits = await docs_index.search(AI_DB, query.strip(), embedder)
    finally:
        await embedder.aclose()
    result = docs_index.format_hits(hits)
    await asyncio.to_thread(tracer.record, "search_rules", {"query": query}, result, None, 0)
    return result


def main() -> None:
    tracer.ensure_table()
    server.run("stdio")
