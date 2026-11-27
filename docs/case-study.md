# Case study: an AI layer over a real cafeteria system

The engine in this repository was first built against **[CantinaPro](https://cantinapro.com.br/users/sign_in)**, a school
cafeteria payments product I founded and run in production (Rails, PostgreSQL, PIX
payments, counter and kiosk sales). Its code and schema are private, so it ran on a local
copy filled with **synthetic data**: two schools and 90 days of activity generated through
the application's own services, so balances and cash registers reconcile the way they do
in production. The demo in this repository rebuilds the same ideas on a fictional schema
anyone can run.

## 1. From a question to an answer

*"Por que a Beatriz ficou com saldo negativo?"* (why is Beatriz's balance negative?)

| Step | Who | What | Technique |
| --- | --- | --- | --- |
| 0 | Claude | receives the tool list and descriptions (`tools/list`) | MCP |
| 1 | Claude | decides it first needs to know *who* Beatriz is | tool use |
| 2 | server | `find_student("Beatriz")`: the school comes from the login, never from the model | SQL |
| 3 | server | `student_statement(id, 60)`: the model widened the window on its own to see the top-up pattern | pre-aggregated SQL |
| 4 | Claude | sees the balance went from R$ 11,50 to −R$ 12,50 on a **lunch**, and asks itself whether the system allows that | |
| 5 | server | `search_rules("lunch charged without balance")` returns the rule: lunch is never refused | RAG |
| 6 | Claude | answers with the fact (the line in the statement) and the rule, and suggests an action (a top-up of at least the debt) | generation |

The server has no LLM. It only runs SQL and a vector search; the model of the user's own
session does the reasoning. In the first end-to-end run, before the rules were indexed,
the model raised the two questions the data could not answer ("doesn't lunch count
against the daily limit?", "is a balance check missing?"), which is exactly what the
rules search answers.

## 2. What the model adds, and where it must become data

*"Quais produtos são fritos?"* (which products are fried?) The database has no such field.
Given the product list, the model classifies from world knowledge. It works for 17
products, with three limits: it is knowledge of the world, not of *this* kitchen; it does
not scale to thousands of products; and "how much fried food did we sell?" needs the
attribute to exist in SQL. The mature path is **enrichment**: the model classifies each
product once, a person reviews, and it becomes a column queries can use.

## 3. Fixed tools versus text-to-SQL

Two ways to give a model access to a database:

- **Fixed tools**: hand-written, tested SQL for known questions.
- **Text-to-SQL**: the model reads the schema and writes the query.

Text-to-SQL reaches questions no tool foresaw, but its errors are silent (the query runs
and returns a plausible wrong number), and isolation can no longer depend on a `WHERE`
clause. Here the safety lives in the database: one Postgres role per school, row-level
security with the school id written into the policy, a column allow-list, read-only
transactions and timeouts. Attacks tested: reading another school's rows, `set_config`
tricks, forbidden columns, writes, slow queries.

### On the real system (synthetic data, 26 questions, Claude Opus 5.5)

| Setup | Answers | Routing | SQL calls | Avg time | Cost |
| --- | --- | --- | --- | --- | --- |
| fixed tools only | 19/19 | 20/20 | – | 10.2 s | $4.04 |
| fixed + SQL | 22/23 | 23/24 | 7 | 11.8 s | $4.83 |
| SQL only, with semantic notes | 23/23 | 24/24 | 36 | 15.8 s | $5.57 |
| SQL only, without notes | 23/23 | 24/24 | 56 | 20.6 s | $6.00 |

What I expected did not happen: with this model and these questions, **text-to-SQL got
everything right, even without the semantic notes**. The notes changed the path, not the
result: without them, the model explored the data first (profiling queries such as
`GROUP BY kind, cancelled`, checking the time zone) and discovered the rules itself, with
56 queries instead of 36 and 30% more time. Fixed tools stayed faster and cheaper, and the
model preferred them when they existed (7 SQL calls in 26 questions). The one routing
miss: after answering with a fixed tool, it ran SQL to double-check.

Limits of the experiment: simple aggregate questions, a strong model, and a schema that
fits in the context. The next tests: subtler traps, a smaller model, and questions whose
answer depends on a rule that is only in the documentation.

### On the demo (27 questions, Claude Opus 5.5)

| Setup | Answers | Routing | Avg time | Cost |
| --- | --- | --- | --- | --- |
| fixed tools + rules search | 21/21 (6 had no tool and went to review) | 22/22 | 16.0 s | $9.52 |
| fixed tools + SQL + rules search | 25/25 | 21/22 | 10.6 s | $5.58 |

With SQL available, the questions no fixed tool covers became one query each: "which
students never topped up by PIX?" went from ~200 statements read one by one (96 s, $3.60)
to 15.7 s and $0.24; "which class spends most on sweets on Fridays?" and "the average
counter sale" went from no answer to right answers. The model kept SQL as a last resort:
it used it where a fixed tool answered in only two questions, both to double-check a
number. The routing miss was my own expectation (a 30-day question for a tool that looks
at one day at a time), and on that question the model pointed out a real ambiguity:
"the last 30 days" can start on either of two dates, and it gave both totals.

The first run with SQL found a bug in the server, not in the model: every SQL call
failed. The model did not invent numbers: it said the SQL tool was down, explained why
the other tools could not replace it, and showed the query it would run. It also showed
a weak check in the scorer (an expected count of 1 matched the "1" of `SELECT 1`): that
case now requires the students' names.

## 4. What the evaluation caught

- **Gaps show up as cost, not errors.** "How many students are negative?" had no tool:
  the model listed every student by first name, ~90 calls, and got it right in 84 s for
  $0.63. With a `students_by_balance` tool: 9.7 s and $0.18. On the demo, "how many never
  topped up by PIX?" without SQL read ~200 statements one by one (96 s, $3.60, right answer).
- **The environment leaked.** Run inside the repository, the model read the server's code
  and cited it in its answers. Each evaluation session now runs in an empty folder, like a
  real user's.
- **Oracles can be wrong too.** One "failure" was the oracle's: a name from another school
  also matched a student here under the search rule. The oracle now uses the same rule.
- **Row-level security has a performance cost you only see on fresh data.** On CI, a stock
  query run as the school's role hit the 5 s statement timeout: right after loading, the
  tables had no planner statistics, and the EXISTS each child-table policy adds got a bad
  plan (3.1 s locally, against 5 ms after). An `ANALYZE` after loading and an index on the
  column the policy joins on fixed it.
- **Isolation held.** Asked to "ignore the configured school", the model refused in every
  setup; with SQL available it queried the schools table and saw only its own: the
  database was the guarantee, not the model's obedience.

## 5. Design choices

- The tenant is configuration, never a tool argument, and every tool connects as the
  tenant's database role.
- Tools return short, pre-aggregated text: the model reads facts and does not add rows.
- Text-to-SQL is a last resort, said in the tool description, and it requires a `reason`
  that is recorded in the trace.
- Every tool call is traced (arguments, result, error, latency).
- The rules corpus is curated: it describes behaviour, never weaknesses of a real system.
