from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from cantina_ai.demo.access import POLICY
from cantina_ai.demo.tools import Movement, brl, with_running_balance
from cantina_ai.engine.evals import Checks, amounts_in, has_amount, score
from cantina_ai.engine.tenant_guard import role_for, setup_sql
from cantina_ai.engine.text_to_sql import SQLInputError, run_select

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def test_the_tenant_id_is_written_into_every_policy() -> None:
    out = setup_sql(POLICY, 7)
    assert role_for(POLICY, 7) == "cantina_school_7"
    assert "USING (school_id = 7)" in out
    assert "ON schools FOR SELECT TO cantina_school_7 USING (id = 7)" in out
    # Never a setting the model's SQL could change.
    assert "current_setting" not in out


def test_tables_without_the_tenant_column_go_through_their_parent() -> None:
    out = setup_sql(POLICY, 1)
    assert (
        "ON transaction_items FOR SELECT TO cantina_school_1 USING "
        "(EXISTS (SELECT 1 FROM transactions p WHERE p.id = transaction_id))"
    ) in out


def test_only_allow_listed_columns_are_granted() -> None:
    out = setup_sql(POLICY, 1)
    assert "GRANT SELECT (id, name, two_step_orders, daily_limit_enabled) ON schools" in out
    assert "GRANT SELECT ON" not in out  # never a whole table


def test_the_role_is_read_only_with_a_timeout() -> None:
    out = setup_sql(POLICY, 1)
    assert "default_transaction_read_only = on" in out
    assert "statement_timeout = '5s'" in out


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM students",
        "UPDATE students SET balance = 0",
        "SELECT 1; DROP TABLE students",
        "  insert into x values (1)",
    ],
)
def test_text_to_sql_refuses_anything_but_one_select(sql: str) -> None:
    with pytest.raises(SQLInputError):
        run_select(None, sql)  # type: ignore[arg-type]  # refused before reaching a connection


def test_amounts_are_read_in_brazilian_format() -> None:
    found = amounts_in("Faturou R$ 1.416,50 em 121 vendas; saldo de -R$ 42,50.")
    assert has_amount(found, Decimal("1416.50"))
    assert has_amount(found, Decimal("-42.50"))  # the sign may be in words
    assert has_amount(found, 121)


def test_score_reports_missing_and_forbidden_values() -> None:
    checks = Checks(
        amounts=[Decimal("10.00")], texts=["almoço"], forbidden_amounts=[Decimal("99.00")]
    )
    assert score(checks, "Foi o almoco de R$ 10,00") == []
    problems = score(checks, "R$ 99,00")
    assert "missing amount 10.00" in problems
    assert any("forbidden" in p for p in problems)


def mv(kind: str, amount: str, minutes: int = 0, paid_in_cash: bool = False) -> Movement:
    return Movement(
        T0 + timedelta(minutes=minutes),
        kind,
        Decimal(amount),
        False,
        "pix",
        False,
        paid_in_cash,
        "",
    )


def test_running_balance_ends_at_the_current_balance() -> None:
    moves = with_running_balance(
        [
            mv("top_up", "50.00"),
            mv("purchase", "45.00", 1),
            mv("purchase", "13.00", 2),
            mv("refund", "13.00", 3),
            mv("purchase", "11.00", 4),
        ],
        Decimal("-6.00"),
    )
    assert [m.balance_after for m in moves] == [
        Decimal(x) for x in ("50.00", "5.00", "-8.00", "5.00", "-6.00")
    ]


def test_a_purchase_paid_in_cash_does_not_move_the_balance() -> None:
    [m] = with_running_balance([mv("purchase", "13.00", paid_in_cash=True)], Decimal("8.00"))
    assert m.effect == 0 and m.balance_after == Decimal("8.00")


def test_brl() -> None:
    assert brl(Decimal("1234.5")) == "R$ 1.234,50"
    assert brl(Decimal("-12.5")) == "-R$ 12,50"
