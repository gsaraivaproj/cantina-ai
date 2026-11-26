"""Tool-call tracing: every MCP tool call is recorded with its arguments, result,
error and latency, so routing, failures and slow tools can be read afterwards.

    tracer = Tracer("dbname=cantina_ai", tenant_id=1)
    result = tracer.run("sales", lambda: tools.sales(conn, ...), {"start": "2026-10-01"})
"""

import json
import time
from collections.abc import Callable
from typing import Any

import psycopg

DDL = """
CREATE TABLE IF NOT EXISTS tool_calls (
    id bigserial PRIMARY KEY,
    called_at timestamptz NOT NULL DEFAULT now(),
    tenant_id int NOT NULL,
    tool text NOT NULL,
    args jsonb NOT NULL,
    result text,
    error text,
    latency_ms int NOT NULL
)
"""


class Tracer:
    def __init__(self, conninfo: str, tenant_id: int) -> None:
        self.conninfo = conninfo
        self.tenant_id = tenant_id

    def ensure_table(self) -> None:
        with psycopg.connect(self.conninfo, autocommit=True) as conn:
            conn.execute(DDL)

    def record(
        self, tool: str, args: dict[str, Any], result: str | None, error: str | None, ms: int
    ) -> None:
        try:
            with psycopg.connect(self.conninfo, autocommit=True) as conn:
                conn.execute(
                    "INSERT INTO tool_calls (tenant_id, tool, args, result, error, latency_ms) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (self.tenant_id, tool, json.dumps(args, default=str), result, error, ms),
                )
        except psycopg.Error:
            pass  # tracing must never break a tool call

    def run(self, tool: str, fn: Callable[[], str], args: dict[str, Any]) -> str:
        start = time.perf_counter()
        try:
            result = fn()
        except Exception as exc:
            self.record(tool, args, None, str(exc), _ms(start))
            raise
        self.record(tool, args, result, None, _ms(start))
        return result


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)
