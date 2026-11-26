"""LLM evaluation harness for MCP tools.

Each case is a question, the tools it should use, and checks on the answer whose
expected values come from ORACLE queries: SQL written independently of the tools
(another path through the data) and run at evaluation time, so nothing is
hard-coded and the set survives a reseed.

Each question is asked to a fresh `claude -p` session, in an empty folder, that only
sees the server's tools (--strict-mcp-config). The stream-json output gives the tools
called, the answer, the cost and the time. Scores:
- routing: every expected tool was called (n/a when no tool is expected);
- answer: every expected amount/text is present, and no forbidden one is;
- review: behaviour a person should read (refusals, clarifications, injections);
- for setups with both kinds of tools: whether SQL was used where a fixed tool answers.
"""

import contextlib
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg

Conn = psycopg.Connection[Any]
# Brazilian number format: 1.234,50 / 36,50 / 9
NUMBER = re.compile(r"-?\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|-?\d+(?:,\d{1,2})?")


@dataclass
class Checks:
    amounts: list[Decimal] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)  # accent/case-insensitive
    any_text: list[str] = field(default_factory=list)  # at least one must appear
    forbidden_amounts: list[Decimal] = field(default_factory=list)
    forbidden_texts: list[str] = field(default_factory=list)
    needs_review: bool = False


@dataclass
class Case:
    id: str
    group: str
    tools: set[str]  # expected fixed tools; empty = no fixed tool answers it
    build: Callable[[Conn, int], tuple[str, Checks]]  # (question, checks) from oracles
    needs_sql: bool = False  # only the SQL tool can answer


@dataclass
class Result:
    id: str
    group: str
    question: str
    expected_tools: list[str]
    tools: list[str]
    routing: str  # ok / missing / n/a
    answer_ok: bool
    problems: list[str]
    review: bool
    sql_unneeded: bool
    answer: str
    cost_usd: float
    seconds: float
    turns: int
    model: str


def fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def amounts_in(text: str) -> list[Decimal]:
    out = []
    for raw in NUMBER.findall(text):
        with contextlib.suppress(InvalidOperation):
            out.append(Decimal(raw.replace(".", "").replace(",", ".")))
    return out


def has_amount(found: list[Decimal], value: Decimal | float | int) -> bool:
    # The sign may be in words ("negativo em R$ 36,50"): compare absolute values.
    target = abs(Decimal(str(value)))
    return any(abs(abs(f) - target) < Decimal("0.005") for f in found)


def score(checks: Checks, answer: str) -> list[str]:
    problems = []
    found, folded = amounts_in(answer), fold(answer)
    problems += [f"missing amount {v}" for v in checks.amounts if not has_amount(found, v)]
    problems += [f"missing text {t!r}" for t in checks.texts if fold(t) not in folded]
    if checks.any_text and not any(fold(t) in folded for t in checks.any_text):
        problems.append(f"none of {checks.any_text!r}")
    problems += [
        f"forbidden amount {v}" for v in checks.forbidden_amounts if v and has_amount(found, v)
    ]
    problems += [f"forbidden text {t!r}" for t in checks.forbidden_texts if fold(t) in folded]
    return problems


@dataclass
class Setup:
    server_name: str  # the MCP server name, tools show up as mcp__<name>__<tool>
    server: dict[str, Any]  # {"command":..., "args":[...], "env":{...}}
    tools: list[str]  # every tool the session may call
    toolset: str = "fixed"  # "fixed" | "fixed+sql" | "sql"
    sql_tool: str = "query_sql"
    model: str | None = None


def ask(question: str, setup: Setup, mcp_config: Path, workdir: Path) -> dict[str, Any]:
    claude = shutil.which("claude")
    assert claude, "claude CLI not found"
    prefix = f"mcp__{setup.server_name}__"
    cmd = [
        claude,
        "-p",
        question,
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--allowedTools",
        ",".join(prefix + t for t in setup.tools),
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if setup.model:
        cmd += ["--model", setup.model]
    proc = subprocess.run(
        cmd, cwd=workdir, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300
    )
    out: dict[str, Any] = {"tools": [], "answer": "", "cost": 0.0, "ms": 0, "turns": 0}
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            out["model"] = event.get("model", "")
        elif kind == "assistant":
            for block in event["message"].get("content", []):
                if block.get("type") == "tool_use" and block["name"].startswith(prefix):
                    out["tools"].append(block["name"].removeprefix(prefix))
        elif kind == "result":
            out.update(
                answer=event.get("result", ""),
                cost=event.get("total_cost_usd", 0.0),
                ms=event.get("duration_ms", 0),
                turns=event.get("num_turns", 0),
            )
    if not out["answer"]:
        out["answer"] = "(no answer) " + proc.stderr[-500:]
    return out


def expected_tools(case: Case, setup: Setup) -> set[str]:
    if setup.toolset == "sql":
        return {setup.sql_tool} if case.tools or case.needs_sql else set()
    if case.needs_sql:
        # With both kinds, a fixed tool plus the model's own reasoning may also answer,
        # so only the answer is scored.
        return set()
    return case.tools


def run_case(
    case: Case,
    setup: Setup,
    oracle: Callable[[], Conn],
    tenant: int,
    mcp_config: Path,
    workdir: Path,
) -> Result:
    with oracle() as conn:
        question, checks = case.build(conn, tenant)
    got = ask(question, setup, mcp_config, workdir)
    used, expected = set(got["tools"]), expected_tools(case, setup)
    routing = "n/a" if not expected else ("ok" if expected <= used else "missing")
    problems = score(checks, got["answer"])
    answer_ok = not problems
    if routing == "missing":
        problems.append(f"did not call {sorted(expected - used)}")
    sql_unneeded = setup.toolset == "fixed+sql" and bool(case.tools) and setup.sql_tool in used
    if sql_unneeded:
        problems.append("used SQL where a fixed tool answers")
    return Result(
        case.id,
        case.group,
        question,
        sorted(expected),
        got["tools"],
        routing,
        answer_ok,
        problems,
        checks.needs_review or (case.needs_sql and "sql" not in setup.toolset),
        sql_unneeded,
        got["answer"],
        got["cost"],
        got["ms"] / 1000,
        got["turns"],
        got.get("model", setup.model or ""),
    )


def run(
    cases: list[Case],
    setup: Setup,
    oracle: Callable[[], Conn],
    tenant: int,
    results_dir: Path,
    parallel: int = 4,
) -> tuple[list[Result], Path]:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"mcpServers": {setup.server_name: setup.server}}, f)
        mcp_config = Path(f.name)
    # An empty folder: the session has the tools, not this repository's code.
    workdir = Path(tempfile.mkdtemp(prefix="cantina-eval-"))
    try:
        with ThreadPoolExecutor(parallel) as pool:
            results = list(
                pool.map(lambda c: run_case(c, setup, oracle, tenant, mcp_config, workdir), cases)
            )
    finally:
        mcp_config.unlink()
    results_dir.mkdir(parents=True, exist_ok=True)
    label = setup.toolset.replace("+", "-")
    path = results_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{label}.json"
    path.write_text(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))
    return results, path


def summary(results: list[Result], label: str) -> str:
    lines = []
    for r in results:
        ok = r.answer_ok and r.routing != "missing" and not r.sql_unneeded
        mark = "REVIEW" if r.review else ("ok" if ok else "FAIL")
        lines.append(
            f"{mark:6} {r.id:28} tools={','.join(r.tools) or '-':40} "
            f"{r.seconds:5.1f}s ${r.cost_usd:.3f}  {'; '.join(r.problems)}"
        )
    scored = [r for r in results if not r.review]
    routed = [r for r in results if r.routing != "n/a"]
    lines.append(
        f"\n[{label}] answers {sum(r.answer_ok for r in scored)}/{len(scored)}  "
        f"routing {sum(r.routing == 'ok' for r in routed)}/{len(routed)}  "
        f"unneeded SQL {sum(r.sql_unneeded for r in results)}  "
        f"review {sum(r.review for r in results)}  "
        f"cost ${sum(r.cost_usd for r in results):.2f}  "
        f"avg {sum(r.seconds for r in results) / max(len(results), 1):.1f}s"
    )
    return "\n".join(lines)
