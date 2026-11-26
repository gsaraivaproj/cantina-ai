"""Calls every tool through the MCP server module against the demo database, as
the school's role: catches wiring bugs the unit tests cannot see. Skipped when the
demo database is not there (run `uv run cantina-demo-seed` first)."""

import os

import psycopg
import pytest

os.environ.setdefault("CANTINA_SCHOOL_ID", "1")
os.environ.setdefault("CANTINA_TOOLSET", "fixed+sql")

try:
    psycopg.connect("dbname=cantina_demo user=cantina_school_1").close()
except psycopg.Error:
    pytest.skip("demo database not available", allow_module_level=True)

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from cantina_ai.demo import server  # noqa: E402


def test_every_fixed_tool_answers() -> None:
    assert "student(s)" in server.find_student("a")
    assert "movements" in server.student_statement(1, 30)
    assert "balance" in server.students_by_balance()
    assert "revenue" in server.sales(group_by="category")
    assert "pace" in server.low_stock()
    server.cash_registers()
    server.pending_orders()


def test_sql_tools_answer_and_stay_in_the_school() -> None:
    assert "students(" in server.describe_database()
    out = server.query_sql("SELECT count(DISTINCT school_id) AS n FROM students", "test")
    assert out.splitlines()[-1] == "1"
    with pytest.raises(ToolError):
        server.query_sql("DELETE FROM students", "test")
    with pytest.raises(ToolError):
        server.query_sql("SELECT 1", "   ")


def test_bad_arguments_come_back_as_tool_errors() -> None:
    with pytest.raises(ToolError):
        server.low_stock(cover_days=90)
