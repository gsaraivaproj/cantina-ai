"""The demo's fixed tools: SQL written and checked by hand against the schema's rules.

Every query filters by the configured school AND runs as that school's database role
(row-level security), so a bug in a WHERE clause still cannot reach another school.
Results are short, pre-aggregated text: the model reads facts instead of adding rows.

Rules these queries encode (and the reason they are fixed tools):
- revenue sums transaction ITEMS of kind purchase/counter_sale, not cancelled
  (cancelled may be NULL); never a header joined to its items;
- a purchase paid in cash has a top-up with pays_transaction_id pointing at it:
  that pair does not move the balance, and that top-up is not new money;
- products sold inside combos are in item_components;
- a withdrawal within two minutes of a register's closed_at is the end-of-day close.
"""

import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

TZ = ZoneInfo("America/Sao_Paulo")
Conn = psycopg.Connection[Any]


class ToolInputError(ValueError):
    """A bad argument, explained to the model so it can fix the call."""


def today() -> date:
    return datetime.now(TZ).date()


def brl(value: Decimal | float | int) -> str:
    """R$ 1.234,50 and -R$ 12,50."""
    v = Decimal(str(value))
    s = f"{abs(v):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return ("-" if v < 0 else "") + "R$ " + s


def fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _day(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ToolInputError(f"invalid date {value!r}: use YYYY-MM-DD") from exc


def _start(d: date) -> datetime:
    return datetime.combine(d, time(0), TZ)


# --- students ------------------------------------------------------------------


def find_student(conn: Conn, school: int, name: str) -> str:
    terms = [fold(t) for t in name.split() if t.strip()]
    if not terms:
        raise ToolInputError("give at least part of the name")
    rows = conn.execute(
        """
        SELECT s.id, s.name, s.grade, s.balance, s.daily_limit, s.active,
               (SELECT string_agg(g.name, ', ') FROM student_guardians sg
                  JOIN guardians g ON g.id = sg.guardian_id WHERE sg.student_id = s.id)
        FROM students s WHERE s.school_id = %s ORDER BY s.name
        """,
        (school,),
    ).fetchall()
    found = [r for r in rows if all(t in fold(r[1]) for t in terms)]
    if not found:
        return f"no student with {name!r} in the name"
    lines = [f"{len(found)} student(s) with {name!r} in the name:"]
    for sid, n, grade, bal, limit, active, guardians in found[:15]:
        extra = [f"daily limit {brl(limit)}" if limit else "", "" if active else "INACTIVE"]
        lines.append(
            f"- id {sid}: {n} ({grade}), balance {brl(bal)}"
            + "".join(f", {x}" for x in extra if x)
            + (f"; guardian: {guardians}" if guardians else "")
        )
    if len(found) > 15:
        lines.append(f"... and {len(found) - 15} more: narrow the name")
    return "\n".join(lines)


@dataclass
class Movement:
    at: datetime
    kind: str
    amount: Decimal
    cancelled: bool
    method: str | None
    is_lunch: bool
    paid_in_cash: bool
    items: str
    balance_after: Decimal = Decimal(0)

    @property
    def effect(self) -> Decimal:
        if self.kind == "purchase":
            return Decimal(0) if self.paid_in_cash else -self.amount
        if self.kind in ("top_up", "refund"):
            return self.amount
        return Decimal(0)


def with_running_balance(moves: list[Movement], current: Decimal) -> list[Movement]:
    """Balance after each movement (oldest first), ending at the current balance."""
    balance = current - sum((m.effect for m in moves), Decimal(0))
    for m in moves:
        balance += m.effect
        m.balance_after = balance
    return moves


def student_statement(conn: Conn, school: int, student_id: int, days: int = 30) -> str:
    if not 1 <= days <= 120:
        raise ToolInputError("'days' must be between 1 and 120")
    st = conn.execute(
        "SELECT name, grade, balance, daily_limit FROM students WHERE id = %s AND school_id = %s",
        (student_id, school),
    ).fetchone()
    if st is None:
        raise ToolInputError(f"student {student_id} not found in this school: use find_student")
    name, grade, balance, limit = st
    rows = conn.execute(
        """
        SELECT t.created_at, t.kind, t.amount, coalesce(t.cancelled, false), t.payment_method,
               t.is_lunch,
               EXISTS (SELECT 1 FROM transactions p WHERE p.pays_transaction_id = t.id),
               coalesce(string_agg(coalesce(p.name, c.name), ' + ' ORDER BY i.id), '')
        FROM transactions t
        LEFT JOIN transaction_items i ON i.transaction_id = t.id
        LEFT JOIN products p ON p.id = i.product_id
        LEFT JOIN combos c ON c.id = i.combo_id
        WHERE t.student_id = %s AND t.school_id = %s AND t.created_at >= %s
          AND t.pays_transaction_id IS NULL      -- the cash payment is folded into its purchase
        GROUP BY t.id ORDER BY t.created_at, t.id
        """,
        (student_id, school, _start(today() - timedelta(days=days))),
    ).fetchall()
    moves = with_running_balance([Movement(*r) for r in rows], Decimal(balance))
    out = [
        f"{name} ({grade}): balance now {brl(balance)}"
        + (f", daily limit {brl(limit)}" if limit else ""),
        f"movements in the last {days} days ({len(moves)}), oldest first; "
        "balance = after each movement:",
    ]
    previous = moves[0].balance_after - moves[0].effect if moves else Decimal(balance)
    for m in moves:
        label = {
            "purchase": "lunch" if m.is_lunch else "purchase",
            "top_up": f"top-up ({m.method})",
            "refund": "refund",
        }.get(m.kind, m.kind)
        notes = []
        if m.paid_in_cash:
            notes.append("paid in cash at the counter, balance not used")
        if m.cancelled:
            notes.append("cancelled (see the refund)")
        if previous >= 0 > m.balance_after:
            notes.append("<- balance went negative here")
        sign = "+" if m.effect > 0 else ""
        out.append(
            f"{m.at.astimezone(TZ):%d/%m %H:%M}  {label}"
            + (f": {m.items}" if m.items and m.kind == "purchase" else "")
            + f"  {sign}{brl(m.effect)}  balance {brl(m.balance_after)}"
            + (f"  ({'; '.join(notes)})" if notes else "")
        )
        previous = m.balance_after
    tops = [m for m in moves if m.kind == "top_up"]
    spent = sum((-m.effect for m in moves if m.kind == "purchase"), Decimal(0))
    out.append(
        f"summary: {len(tops)} top-up(s) totalling {brl(sum((m.amount for m in tops), Decimal(0)))}"
        f"; spent from the balance {brl(spent)}"
        + (f"; last top-up {tops[-1].at.astimezone(TZ):%d/%m}" if tops else "; no top-up")
    )
    return "\n".join(out)


def students_by_balance(conn: Conn, school: int, below: float = 0.0, limit: int = 50) -> str:
    if not 1 <= limit <= 200:
        raise ToolInputError("'limit' must be between 1 and 200")
    threshold = Decimal(str(below))
    rows = conn.execute(
        """
        SELECT s.name, s.grade, s.balance, s.active,
               (SELECT string_agg(g.name, ', ') FROM student_guardians sg
                  JOIN guardians g ON g.id = sg.guardian_id WHERE sg.student_id = s.id),
               (SELECT max(t.created_at) FROM transactions t WHERE t.student_id = s.id
                  AND t.kind = 'top_up' AND t.pays_transaction_id IS NULL)
        FROM students s WHERE s.school_id = %s AND s.balance < %s ORDER BY s.balance, s.name
        """,
        (school, threshold),
    ).fetchall()
    what = "negative" if threshold == 0 else f"below {brl(threshold)}"
    if not rows:
        return f"no student with a {what} balance"
    total = sum((r[2] for r in rows), Decimal(0))
    out = [f"{len(rows)} student(s) with a {what} balance, totalling {brl(total)}:"]
    for n, grade, bal, active, guardians, last in rows[:limit]:
        out.append(
            f"- {n} ({grade}): {brl(bal)}"
            + ("" if active else ", INACTIVE")
            + (f"; guardian: {guardians}" if guardians else "")
            + (f"; last top-up {last.astimezone(TZ):%d/%m}" if last else "; never topped up")
        )
    if len(rows) > limit:
        out.append(f"... and {len(rows) - limit} more")
    return "\n".join(out)


# --- sales -----------------------------------------------------------------------

GROUPS = {
    "day": "to_char(t.created_at AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD (Dy)')",
    "product": "coalesce(p.name, c.name)",
    "category": "coalesce(k.name, CASE WHEN c.id IS NOT NULL THEN 'Combos' END)",
    "sale_type": (
        "CASE WHEN t.is_lunch THEN 'lunch' WHEN t.kind = 'purchase' "
        "THEN 'student (balance)' ELSE 'counter (no student)' END"
    ),
}


def sales(
    conn: Conn,
    school: int,
    start: str | None = None,
    end: str | None = None,
    group_by: str | None = None,
) -> str:
    first = _day(start, today())
    last = _day(end, first)
    if last < first:
        raise ToolInputError("'end' is before 'start'")
    if (last - first).days > 366:
        raise ToolInputError("at most one year")
    if group_by and group_by not in GROUPS:
        raise ToolInputError(f"'group_by' must be one of: {', '.join(GROUPS)}")
    base = sql.SQL("""
        FROM transaction_items i
        JOIN transactions t ON t.id = i.transaction_id
        LEFT JOIN products p ON p.id = i.product_id
        LEFT JOIN categories k ON k.id = p.category_id
        LEFT JOIN combos c ON c.id = i.combo_id
        WHERE t.school_id = %s AND t.kind IN ('purchase', 'counter_sale')
          AND NOT coalesce(t.cancelled, false)
          AND t.created_at >= %s AND t.created_at < %s
    """)
    params = (school, _start(first), _start(last + timedelta(days=1)))
    total = conn.execute(
        sql.SQL("SELECT count(*), count(DISTINCT t.id), coalesce(sum(i.amount), 0) ") + base,
        params,
    ).fetchone()
    assert total
    items, n, revenue = total
    period = f"{first:%d/%m/%Y}" + ("" if last == first else f" to {last:%d/%m/%Y}")
    out = [
        f"sales {period} (today is {today():%d/%m/%Y}): revenue {brl(revenue)}, "
        f"{n} sale(s), {items} item(s); cancelled sales and top-ups are not counted"
    ]
    if group_by and n:
        query = sql.SQL(
            "SELECT {key}, count(*), sum(i.amount) {base} GROUP BY 1 ORDER BY {order} LIMIT 40"
        ).format(
            key=sql.SQL(GROUPS[group_by]),  # pyright: ignore[reportArgumentType]
            base=base,
            order=sql.SQL("1" if group_by == "day" else "3 DESC"),
        )
        out.append(f"by {group_by}:")
        out += [
            f"- {k}: {brl(v)} ({c} item(s))" for k, c, v in conn.execute(query, params).fetchall()
        ]
    return "\n".join(out)


# --- stock, registers, orders ------------------------------------------------------


def low_stock(conn: Conn, school: int, cover_days: int = 5) -> str:
    if not 1 <= cover_days <= 60:
        raise ToolInputError("'cover_days' must be between 1 and 60")
    rows = conn.execute(
        """
        WITH units AS (
            SELECT coalesce(ic.product_id, i.product_id) AS product_id, t.created_at
            FROM transaction_items i
            JOIN transactions t ON t.id = i.transaction_id
            LEFT JOIN item_components ic ON ic.item_id = i.id
            WHERE t.school_id = %(s)s AND t.kind IN ('purchase', 'counter_sale')
              AND NOT coalesce(t.cancelled, false) AND t.created_at >= %(since)s
        ),
        days AS (SELECT greatest(count(DISTINCT date(created_at)), 1) AS n FROM units),
        pace AS (SELECT product_id, count(*) AS sold FROM units GROUP BY 1),
        restock AS (SELECT product_id, max(created_at) AS at FROM stock_entries
                    WHERE school_id = %(s)s GROUP BY 1)
        SELECT p.name, p.stock, coalesce(pace.sold, 0)::numeric / days.n, days.n, restock.at
        FROM products p CROSS JOIN days
        LEFT JOIN pace ON pace.product_id = p.id
        LEFT JOIN restock ON restock.product_id = p.id
        WHERE p.school_id = %(s)s AND p.active AND NOT p.is_lunch
        ORDER BY 1
        """,
        {"s": school, "since": _start(today() - timedelta(days=14))},
    ).fetchall()
    if not rows:
        return "no products"
    risk = []
    for name, qty, per_day, _, last in rows:
        cover = Decimal(qty) / per_day if per_day else None
        if qty <= 0 or (cover is not None and cover < cover_days):
            risk.append((cover or Decimal(0), name, qty, per_day, last))
    head = (
        f"sales pace over {rows[0][3]} school day(s) in the last 14 days; products that "
        f"run out in under {cover_days} school day(s):"
    )
    if not risk:
        return head + "\nnone: stock covers the period for every product"
    out = [head]
    for cover, name, qty, per_day, last in sorted(risk, key=lambda r: r[0]):
        when = "SOLD OUT" if qty <= 0 else f"runs out in ~{cover:.1f} day(s)"
        out.append(
            f"- {name}: {qty} in stock, sells ~{per_day:.1f}/day, {when}"
            + (f"; last restock {last.astimezone(TZ):%d/%m}" if last else "")
        )
    return "\n".join(out)


def cash_registers(conn: Conn, school: int, day: str | None = None) -> str:
    d = _day(day, today())
    regs = conn.execute(
        """
        SELECT r.id, o.name, r.status, r.opened_at, r.closed_at, r.balance,
               coalesce((SELECT sum(CASE WHEN t.kind IN ('top_up', 'counter_sale') THEN t.amount
                                         ELSE -t.amount END)
                         FROM transactions t WHERE t.cash_register_id = r.id), 0)
        FROM cash_registers r JOIN operators o ON o.id = r.operator_id
        WHERE r.school_id = %s AND (r.status = 'open' OR (r.closed_at >= %s AND r.closed_at < %s))
        ORDER BY r.status DESC, r.opened_at
        """,
        (school, _start(d), _start(d + timedelta(days=1))),
    ).fetchall()
    if not regs:
        return f"no register open and none closed on {d:%d/%m/%Y}"
    parts = conn.execute(
        """
        SELECT t.cash_register_id,
               CASE WHEN t.kind = 'top_up' AND t.pays_transaction_id IS NOT NULL
                      THEN 'purchase paid in cash'
                    WHEN t.kind = 'top_up' THEN 'top-up ' || t.payment_method
                    WHEN t.kind = 'counter_sale' THEN 'counter sale'
                    WHEN t.kind = 'withdrawal' AND r.closed_at IS NOT NULL
                         AND t.created_at >= r.closed_at - interval '2 minutes'
                      THEN 'end-of-day close'
                    WHEN t.kind = 'withdrawal' THEN 'withdrawal'
                    ELSE t.kind END,
               count(*),
               sum(CASE WHEN t.kind IN ('top_up', 'counter_sale') THEN t.amount ELSE -t.amount END)
        FROM transactions t JOIN cash_registers r ON r.id = t.cash_register_id
        WHERE t.cash_register_id = ANY(%s)
        GROUP BY 1, 2 ORDER BY 1, 4 DESC
        """,
        ([r[0] for r in regs],),
    ).fetchall()
    by_reg: dict[int, list[str]] = {}
    for rid, kind, n, total in parts:
        by_reg.setdefault(rid, []).append(f"{kind} {brl(total)} ({n})")
    out = [f"cash registers (today is {today():%d/%m/%Y}):"]
    for rid, op, status, opened, closed, bal, net in regs:
        opened = opened.astimezone(TZ)
        if status == "open":
            age = (today() - opened.date()).days
            state = (
                f"OPEN since {opened:%d/%m %H:%M}"
                + (f" ({age} day(s) ago: forgotten?)" if age >= 1 else "")
                + f", balance {brl(bal)}"
            )
        else:
            state = (
                f"closed {opened:%d/%m %H:%M} -> {closed.astimezone(TZ):%H:%M}, "
                f"final balance {brl(bal)}"
            )
        check = "matches" if bal == net else f"DOES NOT MATCH: movements add up to {brl(net)}"
        out.append(f"- register {rid} ({op}): {state}; {check}")
        out.append("  " + "; ".join(by_reg.get(rid, ["no movements"])))
    return "\n".join(out)


def pending_orders(conn: Conn, school: int) -> str:
    two_step = conn.execute(
        "SELECT two_step_orders FROM schools WHERE id = %s", (school,)
    ).fetchone()
    rows = conn.execute(
        """
        SELECT o.id, t.created_at, coalesce(s.name, 'counter sale (no student)'), t.amount,
               coalesce(string_agg(coalesce(p.name, c.name), ' + ' ORDER BY i.id), '')
        FROM orders o
        JOIN transactions t ON t.id = o.transaction_id
        LEFT JOIN students s ON s.id = t.student_id
        LEFT JOIN transaction_items i ON i.transaction_id = t.id
        LEFT JOIN products p ON p.id = i.product_id
        LEFT JOIN combos c ON c.id = i.combo_id
        WHERE o.school_id = %s AND o.status = 'waiting'
        GROUP BY o.id, t.created_at, s.name, t.amount ORDER BY t.created_at
        """,
        (school,),
    ).fetchall()
    note = (
        ""
        if two_step and two_step[0]
        else (" (this school does not use two-step orders: sales are handed over at once)")
    )
    if not rows:
        return "no order waiting" + note
    now = datetime.now(TZ)
    out = [f"{len(rows)} order(s) paid and not handed over yet{note}:"]
    for oid, at, who, amount, items in rows:
        hours = (now - at).total_seconds() / 3600
        wait = f"{hours:.0f} h ago" if hours >= 1 else f"{hours * 60:.0f} min ago"
        out.append(
            f"- order {oid}, {at.astimezone(TZ):%d/%m %H:%M} ({wait}): {who}, "
            f"{items}, {brl(amount)}"
        )
    return "\n".join(out)
