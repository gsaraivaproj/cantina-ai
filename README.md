# cantina-ai

AI over a school cafeteria system: **MCP tools, RAG, guarded text-to-SQL and LLM
evaluation**, built from scratch and measured.

The demo is a fictional cafeteria where students keep a prepaid balance and buy at the
counter or at kiosks. It is inspired by [CantinaPro](#about-cantinapro), a cafeteria
payments product I built and run in production; the same engine runs against that real
system (with synthetic data), whose numbers are in the [case study](docs/case-study.md).

Ask Claude *"Por que a Cecília (o nome muda com o seed) ficou com saldo negativo? O sistema devia ter
bloqueado?"* and it finds the student, reads her statement (the line where the balance
went negative is a lunch), searches the cafeteria's rules (lunch is charged even
without balance, on purpose), and answers with both.

## What is inside

| Piece | What it does | Where |
| --- | --- | --- |
| MCP server | 10 tools over stdio; the school is configuration, never a tool argument; every call traced | `src/cantina_ai/demo/server.py` |
| Fixed tools | Hand-written SQL for the questions an owner asks: revenue, statements, balances, stock, cash registers, orders; results pre-aggregated so the model reads facts instead of adding rows | `src/cantina_ai/demo/tools.py` |
| Text-to-SQL | A last-resort tool for questions no fixed tool answers, made safe in the database: one Postgres role per school, row-level security with the id written into the policy, a column allow-list, read-only, timeouts | `src/cantina_ai/engine/tenant_guard.py`, `text_to_sql.py` |
| RAG | The cafeteria's rule documents, heading-aware chunks, local embeddings (bge-m3 on Ollama), pgvector | `src/cantina_ai/engine/docs_index.py` |
| Evaluation | Questions whose expected values come from independent oracle queries; each question in a fresh agent session; scored on answer, tool routing, cost and latency | `src/cantina_ai/engine/evals.py`, `demo/cases.py` |
| Synthetic data | Two schools, 90 days of top-ups, purchases, lunches, cash payments, refunds, withdrawals, register closes and restocks, with balances that reconcile | `src/cantina_ai/demo/seed.py` |

## Results

On the demo, 27 questions, each in a fresh Claude Code session (Claude Opus 5.5):

| Setup | Answers | Tool routing | Avg time | Cost |
| --- | --- | --- | --- | --- |
| fixed tools + rules search | 21/21 | 22/22 | 16.0 s | $9.52 |
| fixed tools + SQL + rules search | 25/25 | 21/22 | 10.6 s | $5.58 |

The gaps in the fixed tools showed up as cost before they showed up as errors: without
SQL, one question was answered right by reading ~200 statements (96 s, $3.60); with SQL,
in 15.7 s for $0.24. On the real system the numbers were similar, including a comparison
of text-to-SQL with and without the semantic notes. Details in the
[case study](docs/case-study.md).

## Run it

Requirements: PostgreSQL 16+ with pgvector, Ollama, Python 3.14 with [uv](https://docs.astral.sh/uv/), and Claude Code.

```bash
uv sync
createdb cantina_demo && createdb cantina_ai
uv run cantina-demo-seed           # schema + 90 days of data + one database role per school
uv run cantina-demo-index          # embeds the rule documents (pulls bge-m3 the first time)

claude mcp add cantina --scope local -e CANTINA_SCHOOL_ID=1 -- \
  uv --directory "$PWD" run --quiet cantina-mcp
```

Then ask, in Portuguese or English: *"Quanto faturamos ontem?"*, *"O que está acabando
no estoque?"*, *"Os caixas de ontem fecharam certo?"*, *"Qual turma gasta mais com
doces às sextas?"*.

Evaluate (each question costs one Claude Code session):

```bash
CANTINA_SCHOOL_ID=1 uv run cantina-demo-eval                       # fixed tools + RAG
CANTINA_SCHOOL_ID=1 uv run cantina-demo-eval --toolset fixed+sql
CANTINA_SCHOOL_ID=1 uv run cantina-demo-eval --toolset sql [--no-notes]
```

## About CantinaPro

[CantinaPro](https://cantinapro.com.br/users/sign_in) is a payments platform for school
cafeterias (prepaid balances, PIX top-ups, counter and kiosk sales, cash registers) that I
founded and develop. Its code and schema
are private; this repository holds the reusable engine and a fictional demo built on the
same ideas.

## License

MIT
