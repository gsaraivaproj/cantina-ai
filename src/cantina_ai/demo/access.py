"""Who may read what in the demo database, and what the data means.

`POLICY` drives the per-school role (engine/tenant_guard.py): every tool, fixed or
text-to-SQL, runs as the role of the configured school, so isolation never depends on
a WHERE clause. `NOTES` is the semantic layer sent with the schema to text-to-SQL.

    uv run cantina-demo-roles 1     # print the SQL that creates school 1's role
"""

import sys

from cantina_ai.engine.tenant_guard import TenantPolicy, role_for, setup_sql

POLICY = TenantPolicy(
    database="cantina_demo",
    tenant_table="schools",
    tenant_column="school_id",
    role_prefix="cantina_school",
    columns={
        "schools": ["id", "name", "two_step_orders", "daily_limit_enabled"],
        "operators": ["id", "school_id", "name"],
        "guardians": ["id", "school_id", "name"],
        "students": [
            "id",
            "school_id",
            "name",
            "grade",
            "balance",
            "daily_limit",
            "active",
            "created_at",
        ],
        "student_guardians": ["student_id", "guardian_id"],
        "categories": ["id", "school_id", "name"],
        "products": [
            "id",
            "school_id",
            "category_id",
            "name",
            "stock",
            "variable_price",
            "is_lunch",
            "active",
        ],
        "combos": ["id", "school_id", "name"],
        "combo_slots": ["combo_id", "category_id"],
        "menus": ["id", "school_id", "name", "active", "created_at"],
        "menu_prices": ["menu_id", "product_id", "combo_id", "price"],
        "cash_registers": [
            "id",
            "school_id",
            "operator_id",
            "status",
            "balance",
            "opened_at",
            "closed_at",
        ],
        "transactions": [
            "id",
            "school_id",
            "kind",
            "student_id",
            "operator_id",
            "cash_register_id",
            "amount",
            "payment_method",
            "is_lunch",
            "pays_transaction_id",
            "refund_of_id",
            "cancelled",
            "cancelled_at",
            "created_at",
        ],
        "transaction_items": ["id", "transaction_id", "product_id", "combo_id", "amount"],
        "item_components": ["item_id", "product_id"],
        "orders": ["id", "school_id", "transaction_id", "status", "created_at", "delivered_at"],
        "stock_entries": [
            "id",
            "school_id",
            "product_id",
            "quantity",
            "unit_cost",
            "supplier",
            "created_at",
        ],
    },
    parents={
        "student_guardians": "EXISTS (SELECT 1 FROM students p WHERE p.id = student_id)",
        "combo_slots": "EXISTS (SELECT 1 FROM combos p WHERE p.id = combo_id)",
        "menu_prices": "EXISTS (SELECT 1 FROM menus p WHERE p.id = menu_id)",
        "transaction_items": "EXISTS (SELECT 1 FROM transactions p WHERE p.id = transaction_id)",
        "item_components": "EXISTS (SELECT 1 FROM transaction_items p WHERE p.id = item_id)",
    },
)

NOTES = """
Notes on the data (read before writing SQL):
- You only see ONE school: the database already filters every table; filtering by school_id changes nothing.
- students: balance is the prepaid balance now (it can be negative); daily_limit NULL = no limit; grade is the class ("3º ano A"). Guardians are linked through student_guardians.
- A transaction has a HEADER (transactions) and LINES (transaction_items, one per item sold, joined by transaction_id). kind:
  purchase = a student buying with the balance; counter_sale = a sale with no student (student_id NULL);
  top_up = money into a student's balance (payment_method 'pix' or 'cash'), EXCEPT when pays_transaction_id is set: then it is the cash payment of that purchase at the counter, and neither row moved the balance;
  refund = balance given back for a cancelled sale (refund_of_id points at it); withdrawal = money taken out of a cash register.
- REVENUE = sum of transaction_items.amount whose transaction has kind IN ('purchase','counter_sale') and coalesce(cancelled, false) = false. Sum the LINES; never sum transactions.amount in a join with the lines (it repeats once per line). cancelled may be NULL.
- Combos: a line has product_id (single product) or combo_id (a combo); the products chosen inside a combo are in item_components (item_id, product_id). Category: products.category_id -> categories.name (Salgados, Bebidas, Doces, Outros, Almoço).
- Lunch: transactions.is_lunch = true; lunch has a variable price and is charged even when the balance is not enough, so it is the usual reason a balance goes negative.
- Cash registers: balance = top-ups and counter sales received minus withdrawals and counter refunds. A withdrawal within two minutes before a register's closed_at is the end-of-day close; the others are mid-day withdrawals.
- Prices: menu_prices of the menu with active = true. Unit costs: stock_entries.unit_cost. Stock now: products.stock.
- Orders: status 'waiting' = paid, not handed over yet; 'delivered'; 'cancelled'.
- Dates are timestamptz; for days in Brazil use created_at AT TIME ZONE 'America/Sao_Paulo'. Day of week: extract(isodow ...) (1 = Monday, 5 = Friday).
- Aggregate in SQL: at most 200 rows come back.
""".strip()


def roles_main() -> None:
    school = int(sys.argv[1])
    print(f"-- role {role_for(POLICY, school)}")
    print(setup_sql(POLICY, school))
