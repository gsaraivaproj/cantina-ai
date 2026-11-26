"""Synthetic data for the demo cafeteria: two schools, ~90 days of activity.

Deterministic (fixed seed). The business rules are simulated in memory and the
result is written in one transaction, so balances, cash registers, stock and orders
are consistent the way they would be in a real system:

- top-ups (PIX or cash) at the counter go into the operator's open cash register;
- a purchase debits the student's balance; kiosks refuse it when the balance (or
  the daily limit) is not enough, but the counter sometimes sells anyway, so
  balances can go negative; sometimes the student pays in cash instead;
- lunch has a variable price and is charged even without balance;
- cancelling a purchase refunds the balance with a new transaction;
- registers get mid-day withdrawals and are closed at the end of the day with a
  withdrawal of everything left; on the last day one register is left open;
- stock goes down with each product sold (also inside combos) and is restocked
  every Monday; one product is restocked short at the end, so it runs out.

    uv run cantina-demo-seed            # recreate the schema and load the data
"""

import os
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import LiteralString, cast
from zoneinfo import ZoneInfo

import psycopg

SEED = 20261126
DAYS = 90
TZ = ZoneInfo("America/Sao_Paulo")
SCHEMA = Path(__file__).parent / "db" / "schema.sql"

FIRST = [
    "Ana",
    "Beatriz",
    "Bruno",
    "Caio",
    "Carolina",
    "Davi",
    "Eduarda",
    "Enzo",
    "Felipe",
    "Gabriela",
    "Gustavo",
    "Helena",
    "Heitor",
    "Isabela",
    "João",
    "Júlia",
    "Laura",
    "Lorenzo",
    "Lucas",
    "Luiza",
    "Manuela",
    "Marina",
    "Mateus",
    "Miguel",
    "Nicolas",
    "Olívia",
    "Pedro",
    "Rafael",
    "Sofia",
    "Theo",
    "Valentina",
    "Vitor",
    "Yasmin",
    "Arthur",
    "Alice",
    "Bernardo",
    "Cecília",
    "Lara",
    "Murilo",
    "Rebeca",
]
LAST = [
    "Almeida",
    "Barbosa",
    "Cardoso",
    "Carvalho",
    "Castro",
    "Correia",
    "Costa",
    "Dias",
    "Fernandes",
    "Ferreira",
    "Gomes",
    "Lima",
    "Martins",
    "Melo",
    "Moreira",
    "Nunes",
    "Oliveira",
    "Pereira",
    "Pinto",
    "Ramos",
    "Ribeiro",
    "Rocha",
    "Santos",
    "Silva",
    "Souza",
    "Teixeira",
    "Vieira",
]
ADULT = [
    "Adriana",
    "Alexandre",
    "Andreia",
    "Cláudia",
    "Daniel",
    "Fábio",
    "Fernanda",
    "Juliana",
    "Leandro",
    "Marcelo",
    "Márcia",
    "Patrícia",
    "Renata",
    "Ricardo",
    "Rodrigo",
    "Sandra",
    "Tatiana",
    "Vanessa",
]

# name, category, price, unit cost, target stock
PRODUCTS = [
    ("Coxinha", "Salgados", "6.50", "2.60", 120),
    ("Pão de queijo", "Salgados", "4.50", "1.50", 200),
    ("Esfiha de carne", "Salgados", "6.00", "2.40", 100),
    ("Enroladinho de salsicha", "Salgados", "6.00", "2.20", 100),
    ("Pastel assado de frango", "Salgados", "7.00", "2.80", 80),
    ("Misto quente", "Salgados", "8.00", "3.20", 60),
    ("Suco natural de laranja", "Bebidas", "6.00", "2.00", 90),
    ("Refrigerante lata", "Bebidas", "6.00", "2.90", 150),
    ("Água mineral", "Bebidas", "3.50", "1.00", 200),
    ("Achocolatado", "Bebidas", "4.50", "1.80", 150),
    ("Chá gelado", "Bebidas", "5.00", "2.00", 80),
    ("Brigadeiro", "Doces", "3.00", "0.90", 150),
    ("Bolo de pote", "Doces", "8.00", "3.00", 40),
    ("Barra de cereal", "Doces", "4.00", "1.60", 120),
    ("Pipoca doce", "Doces", "4.00", "1.20", 80),
    ("Fruta da estação", "Outros", "3.50", "1.40", 60),
    ("Iogurte", "Outros", "5.00", "2.30", 70),
]
SHORT_STOCK = "Bolo de pote"  # restocked short in the last three weeks

SCHOOLS = [
    {
        "name": "Escola Ipê",
        "students": 180,
        "operators": 2,
        "lunch": True,
        "two_step": True,
        "daily_limit": True,
        "grades": [
            "1º ano A",
            "1º ano B",
            "2º ano A",
            "3º ano A",
            "4º ano A",
            "5º ano A",
            "5º ano B",
        ],
    },
    {
        "name": "Colégio Jacarandá",
        "students": 120,
        "operators": 1,
        "lunch": False,
        "two_step": False,
        "daily_limit": False,
        "grades": ["6º ano", "7º ano", "8º ano", "9º ano", "1ª série EM", "2ª série EM"],
    },
]

CENT = Decimal("0.01")


def money(v: Decimal | str | float) -> Decimal:
    return Decimal(str(v)).quantize(CENT, ROUND_HALF_UP)


@dataclass
class Student:
    id: int
    appetite: float
    favorites: list[str]
    top_up: Decimal
    daily_limit: Decimal | None
    leaves_on: int | None
    balance: Decimal = Decimal(0)
    active: bool = True
    spent: dict[date, Decimal] = field(default_factory=dict)


@dataclass
class Register:
    id: int
    operator_id: int
    balance: Decimal = Decimal(0)


class Seeder:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self.rng = random.Random(SEED)
        self.stats: dict[str, int] = {}
        today = datetime.now(TZ).date()
        first = today - timedelta(days=DAYS)
        mid = today - timedelta(days=DAYS // 2)
        breaks = {mid + timedelta(days=i) for i in range(5)} | {today - timedelta(days=20)}
        self.days = [
            first + timedelta(days=i)
            for i in range(DAYS)
            if (first + timedelta(days=i)).weekday() < 5 and first + timedelta(days=i) not in breaks
        ]

    # --- helpers -------------------------------------------------------------

    def count(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def one(self, sql: str, params: tuple) -> int:
        row = self.conn.execute(sql, params).fetchone()  # pyright: ignore[reportArgumentType]
        assert row
        return row[0]

    def at(self, day: date, start: str, end: str) -> datetime:
        h1, m1 = map(int, start.split(":"))
        h2, m2 = map(int, end.split(":"))
        base = datetime.combine(day, time(h1, m1), TZ)
        return base + timedelta(seconds=self.rng.randrange((h2 * 60 + m2 - h1 * 60 - m1) * 60))

    def name(self) -> str:
        return f"{self.rng.choice(FIRST)} {self.rng.choice(LAST)} {self.rng.choice(LAST)}"

    # --- build ---------------------------------------------------------------

    def build(self, cfg: dict) -> dict:
        c = self.conn
        sid = self.one(
            "INSERT INTO schools (name, two_step_orders, daily_limit_enabled, created_at) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (
                cfg["name"],
                cfg["two_step"],
                cfg["daily_limit"],
                datetime.combine(self.days[0] - timedelta(days=10), time(8), TZ),
            ),
        )
        ops = [
            self.one(
                "INSERT INTO operators (school_id, name) VALUES (%s, %s) RETURNING id",
                (sid, f"{self.rng.choice(ADULT)} {self.rng.choice(LAST)}"),
            )
            for _ in range(cfg["operators"])
        ]
        cats = {
            n: self.one(
                "INSERT INTO categories (school_id, name) VALUES (%s, %s) RETURNING id", (sid, n)
            )
            for n in ["Salgados", "Bebidas", "Doces", "Outros", "Almoço"]
        }
        products = {}
        for name, cat, price, cost, target in PRODUCTS:
            pid = self.one(
                "INSERT INTO products (school_id, category_id, name, stock) "
                "VALUES (%s, %s, %s, 0) RETURNING id",
                (sid, cats[cat], name),
            )
            products[name] = {
                "id": pid,
                "cat": cat,
                "price": money(price),
                "cost": money(cost),
                "target": target,
                "stock": 0,
            }
        lunch = None
        if cfg["lunch"]:
            lunch = self.one(
                "INSERT INTO products (school_id, category_id, name, stock, variable_price, "
                "is_lunch) VALUES (%s, %s, 'Almoço', 0, true, true) RETURNING id",
                (sid, cats["Almoço"]),
            )
        combo = self.one(
            "INSERT INTO combos (school_id, name) VALUES (%s, %s) RETURNING id",
            (sid, "Lanche completo (salgado + bebida)"),
        )
        for cat in ("Salgados", "Bebidas"):
            c.execute("INSERT INTO combo_slots VALUES (%s, %s)", (combo, cats[cat]))
        students = []
        for _ in range(cfg["students"]):
            st = Student(
                id=0,
                appetite=self.rng.choice([0.15, 0.35, 0.55, 0.75]),
                favorites=self.rng.sample([p[0] for p in PRODUCTS], 3),
                top_up=money(self.rng.choice([50, 80, 100, 150])),
                daily_limit=money(self.rng.choice([15, 20, 25]))
                if self.rng.random() < 0.2
                else None,
                leaves_on=int(self.rng.random() * DAYS * 2 / 3)
                if self.rng.random() < 0.03
                else None,
            )
            st.id = self.one(
                "INSERT INTO students (school_id, name, grade, daily_limit, created_at) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (
                    sid,
                    self.name(),
                    self.rng.choice(cfg["grades"]),
                    st.daily_limit,
                    datetime.combine(self.days[0] - timedelta(days=10), time(8), TZ),
                ),
            )
            students.append(st)
        for i in range(0, len(students), 3):
            group = students[i : i + 3]
            for children in (group[:2], group[2:]):
                if not children:
                    continue
                gid = self.one(
                    "INSERT INTO guardians (school_id, name) VALUES (%s, %s) RETURNING id",
                    (sid, f"{self.rng.choice(ADULT)} {self.rng.choice(LAST)}"),
                )
                for ch in children:
                    c.execute("INSERT INTO student_guardians VALUES (%s, %s)", (ch.id, gid))
        school = {
            "id": sid,
            "cfg": cfg,
            "ops": ops,
            "products": products,
            "lunch": lunch,
            "combo": combo,
            "students": students,
            "registers": {},
            "menu": None,
            "prices": {},
        }
        self.new_menu(school, "Cardápio", Decimal(1), datetime.combine(self.days[0], time(6), TZ))
        return school

    def new_menu(self, school: dict, name: str, factor: Decimal, when: datetime) -> None:
        c = self.conn
        if school["menu"]:
            c.execute("UPDATE menus SET active = false WHERE id = %s", (school["menu"],))
        mid = self.one(
            "INSERT INTO menus (school_id, name, active, created_at) "
            "VALUES (%s, %s, true, %s) RETURNING id",
            (school["id"], name, when),
        )
        school["menu"] = mid
        for name_, p in school["products"].items():
            price = (p["price"] * factor * 2).quantize(Decimal(1), ROUND_HALF_UP) / 2
            school["prices"][name_] = money(price)
            c.execute(
                "INSERT INTO menu_prices (menu_id, product_id, price) VALUES (%s, %s, %s)",
                (mid, p["id"], price),
            )
        combo_price = money((Decimal(11) * factor * 2).quantize(Decimal(1), ROUND_HALF_UP) / 2)
        school["prices"]["__combo"] = combo_price
        c.execute(
            "INSERT INTO menu_prices (menu_id, combo_id, price) VALUES (%s, %s, %s)",
            (mid, school["combo"], combo_price),
        )

    # --- money movements -------------------------------------------------------

    def register(self, school: dict, op: int, when: datetime) -> Register:
        reg = school["registers"].get(op)
        if reg is None:
            rid = self.one(
                "INSERT INTO cash_registers (school_id, operator_id, status, opened_at) "
                "VALUES (%s, %s, 'open', %s) RETURNING id",
                (school["id"], op, when),
            )
            reg = school["registers"][op] = Register(rid, op)
        return reg

    def tx(self, school: dict, kind: str, amount: Decimal, when: datetime, **cols) -> int:
        cols = {
            "school_id": school["id"],
            "kind": kind,
            "amount": amount,
            "created_at": when,
            **cols,
        }
        names = ", ".join(cols)
        marks = ", ".join(["%s"] * len(cols))
        return self.one(  # column names come from this module, never from input
            f"INSERT INTO transactions ({names}) VALUES ({marks}) RETURNING id",  # noqa: S608
            tuple(cols.values()),
        )

    def top_up(
        self,
        school: dict,
        st: Student,
        amount: Decimal,
        method: str,
        when: datetime,
        pays: int | None = None,
    ) -> None:
        op = self.rng.choice(school["ops"])
        reg = self.register(school, op, when)
        reg.balance += amount
        if pays is None:
            st.balance += amount
        self.tx(
            school,
            "top_up",
            amount,
            when,
            student_id=st.id,
            operator_id=op,
            cash_register_id=reg.id,
            payment_method=method,
            pays_transaction_id=pays,
        )
        self.conn.execute(
            "UPDATE cash_registers SET balance = %s WHERE id = %s", (reg.balance, reg.id)
        )

    def basket(self, school: dict, st: Student | None) -> list[dict]:
        prods = school["products"]
        if self.rng.random() < 0.2:
            parts = [
                self.rng.choice([n for n, p in prods.items() if p["cat"] == cat])
                for cat in ("Salgados", "Bebidas")
            ]
            if any(prods[n]["stock"] <= 0 for n in parts):
                return []
            return [{"combo": True, "parts": parts, "amount": school["prices"]["__combo"]}]
        names = st.favorites if st else list(prods)
        items = []
        for _ in range(self.rng.randint(1, 2)):
            n = self.rng.choice(names) if self.rng.random() < 0.8 else self.rng.choice(list(prods))
            if prods[n]["stock"] > len([i for i in items if i.get("name") == n]):
                items.append({"name": n, "amount": school["prices"][n]})
        return items

    def sell(
        self,
        school: dict,
        st: Student | None,
        items: list[dict],
        when: datetime,
        lunch_price: Decimal | None = None,
        paid_in_cash: bool = False,
    ) -> int:
        c = self.conn
        op = self.rng.choice(school["ops"])
        total = (
            lunch_price
            if lunch_price is not None
            else sum((i["amount"] for i in items), Decimal(0))
        )
        assert total is not None
        if st is None:
            reg = self.register(school, op, when)
            reg.balance += total
            tid = self.tx(
                school,
                "counter_sale",
                total,
                when,
                operator_id=op,
                cash_register_id=reg.id,
                payment_method=self.rng.choice(["cash", "cash", "pix"]),
            )
            c.execute("UPDATE cash_registers SET balance = %s WHERE id = %s", (reg.balance, reg.id))
        else:
            tid = self.tx(
                school,
                "purchase",
                total,
                when,
                student_id=st.id,
                operator_id=op,
                is_lunch=lunch_price is not None,
            )
            if paid_in_cash:
                self.top_up(school, st, total, "cash", when + timedelta(seconds=1), pays=tid)
            else:
                st.balance -= total
                if lunch_price is None:  # lunch does not count against the daily limit
                    st.spent[when.date()] = st.spent.get(when.date(), Decimal(0)) + total
        prods = school["products"]
        if lunch_price is not None:
            c.execute(
                "INSERT INTO transaction_items (transaction_id, product_id, amount) "
                "VALUES (%s, %s, %s)",
                (tid, school["lunch"], lunch_price),
            )
        for it in items:
            if it.get("combo"):
                iid = self.one(
                    "INSERT INTO transaction_items (transaction_id, combo_id, amount) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (tid, school["combo"], it["amount"]),
                )
                for n in it["parts"]:
                    c.execute("INSERT INTO item_components VALUES (%s, %s)", (iid, prods[n]["id"]))
                    prods[n]["stock"] -= 1
            else:
                c.execute(
                    "INSERT INTO transaction_items (transaction_id, product_id, amount) "
                    "VALUES (%s, %s, %s)",
                    (tid, prods[it["name"]]["id"], it["amount"]),
                )
                prods[it["name"]]["stock"] -= 1
        two_step = school["cfg"]["two_step"]
        delivered = when + timedelta(seconds=self.rng.randint(60, 480))
        c.execute(
            "INSERT INTO orders (school_id, transaction_id, status, created_at, delivered_at) "
            "VALUES (%s, %s, 'delivered', %s, %s)",
            (school["id"], tid, when, delivered if two_step else when),
        )
        self.count("purchases" if st else "counter sales")
        if not paid_in_cash and lunch_price is None and self.rng.random() < 0.01:
            self.cancel(school, tid, st, items, total, when + timedelta(minutes=5))
        return tid

    def cancel(
        self,
        school: dict,
        tid: int,
        st: Student | None,
        items: list[dict],
        total: Decimal,
        when: datetime,
    ) -> None:
        c = self.conn
        c.execute(
            "UPDATE transactions SET cancelled = true, cancelled_at = %s WHERE id = %s", (when, tid)
        )
        c.execute(
            "UPDATE orders SET status = 'cancelled', delivered_at = NULL WHERE transaction_id = %s",
            (tid,),
        )
        op = self.rng.choice(school["ops"])
        if st is not None:
            st.balance += total
            st.spent[when.date()] -= total
            self.tx(
                school, "refund", total, when, student_id=st.id, operator_id=op, refund_of_id=tid
            )
        else:
            reg = self.register(school, op, when)
            reg.balance -= total
            self.tx(
                school,
                "refund",
                total,
                when,
                operator_id=op,
                cash_register_id=reg.id,
                refund_of_id=tid,
            )
            c.execute("UPDATE cash_registers SET balance = %s WHERE id = %s", (reg.balance, reg.id))
        prods = school["products"]
        for it in items:
            for n in it.get("parts", [it.get("name")]):
                prods[n]["stock"] += 1
        self.count("cancelled sales")

    def withdraw(self, school: dict, reg: Register, amount: Decimal, when: datetime) -> None:
        reg.balance -= amount
        self.tx(
            school, "withdrawal", amount, when, operator_id=reg.operator_id, cash_register_id=reg.id
        )
        self.conn.execute(
            "UPDATE cash_registers SET balance = %s WHERE id = %s", (reg.balance, reg.id)
        )

    # --- the simulation ----------------------------------------------------------

    def restock(self, school: dict, day: date) -> None:
        when = self.at(day, "06:00", "06:45")
        for name, p in school["products"].items():
            target = p["target"]
            if name == SHORT_STOCK and day > self.days[-1] - timedelta(days=21):
                target //= 4
            missing = target - p["stock"]
            if missing <= 0:
                continue
            p["stock"] += missing
            supplier = (
                "Padaria Trigo" if p["cat"] in ("Salgados", "Doces") else "Distribuidora Sabor"
            )
            self.conn.execute(
                "INSERT INTO stock_entries (school_id, product_id, quantity, unit_cost, supplier, "
                "created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (school["id"], p["id"], missing, p["cost"], supplier, when),
            )
            self.count("restocks")

    def day(self, school: dict, day: date, index: int) -> None:
        cfg = school["cfg"]
        if day == self.days[len(self.days) // 2]:
            self.new_menu(
                school, "Cardápio reajustado", Decimal("1.08"), self.at(day, "06:30", "06:59")
            )
        if day.weekday() == 0 or index == 0:
            self.restock(school, day)
        for st in school["students"]:
            if st.leaves_on == index and st.active:
                st.active = False
                self.conn.execute("UPDATE students SET active = false WHERE id = %s", (st.id,))
            if st.active and st.balance < 15 and self.rng.random() < 0.5:
                method = "pix" if self.rng.random() < 0.65 else "cash"
                self.top_up(school, st, st.top_up, method, self.at(day, "07:00", "08:00"))
                self.count("top-ups")
        boost = 1.25 if day.weekday() == 4 else 1.0
        for st in school["students"]:
            if not st.active or self.rng.random() >= st.appetite * boost:
                continue
            items = self.basket(school, st)
            if not items:
                continue
            total = sum((i["amount"] for i in items), Decimal(0))
            when = (
                self.at(day, "09:40", "10:10")
                if self.rng.random() < 0.7
                else self.at(day, "15:00", "15:30")
            )
            available = st.balance
            if cfg["daily_limit"] and st.daily_limit is not None:
                available = min(available, st.daily_limit - st.spent.get(day, Decimal(0)))
            if total <= available:
                self.sell(school, st, items, when)
            elif self.rng.random() < 0.15:
                self.sell(school, st, items, when, paid_in_cash=True)
                self.count("paid in cash")
            elif self.rng.random() < 0.08:
                self.sell(school, st, items, when)  # the counter sells anyway
                self.count("sold without balance")
            else:
                self.count("refused (no balance)")
        for _ in range(self.rng.randint(8, 18)):
            items = self.basket(school, None)
            if items:
                self.sell(school, None, items, self.at(day, "09:30", "16:00"))
        if school["lunch"]:
            active = [s for s in school["students"] if s.active]
            for st in self.rng.sample(active, self.rng.randint(18, 32)):
                price = money(self.rng.choice([22, 24, 26, 28]))
                self.sell(school, st, [], self.at(day, "11:45", "13:00"), lunch_price=price)
                self.count("lunches")
        if self.rng.random() < 0.3:
            reg = school["registers"].get(self.rng.choice(school["ops"]))
            if reg and reg.balance > 200:
                self.withdraw(
                    school,
                    reg,
                    (reg.balance * Decimal("0.6")).quantize(Decimal(1)),
                    self.at(day, "12:00", "14:00"),
                )
                self.count("withdrawals")
        last = index == len(self.days) - 1
        for i, (op, reg) in enumerate(list(school["registers"].items())):
            if last and i == 0 and len(school["ops"]) > 1:
                self.count("registers left open")
                continue
            when = self.at(day, "17:00", "17:40")
            if reg.balance > 0:
                self.withdraw(school, reg, reg.balance, when)
            self.conn.execute(
                "UPDATE cash_registers SET status = 'closed', closed_at = %s WHERE id = %s",
                (when + timedelta(seconds=30), reg.id),
            )
            del school["registers"][op]
            self.count("registers closed")

    def run(self) -> dict[str, int]:
        schools = [self.build(cfg) for cfg in SCHOOLS]
        for school in schools:
            for i, d in enumerate(self.days):
                self.day(school, d, i)
            for st in school["students"]:
                self.conn.execute(
                    "UPDATE students SET balance = %s WHERE id = %s", (st.balance, st.id)
                )
            for p in school["products"].values():
                self.conn.execute(
                    "UPDATE products SET stock = %s WHERE id = %s", (p["stock"], p["id"])
                )
            if school["cfg"]["two_step"]:
                last = self.days[-1]
                pending = self.conn.execute(
                    "SELECT o.id FROM orders o JOIN transactions t ON t.id = o.transaction_id "
                    "WHERE o.school_id = %s AND o.status = 'delivered' AND t.created_at "
                    "BETWEEN %s AND %s ORDER BY o.id LIMIT 4",
                    (
                        school["id"],
                        datetime.combine(last, time(15), TZ),
                        datetime.combine(last, time(16), TZ),
                    ),
                ).fetchall()
                self.conn.execute(
                    "UPDATE orders SET status = 'waiting', delivered_at = NULL WHERE id = ANY(%s)",
                    ([r[0] for r in pending],),
                )
                self.count("orders left waiting", len(pending))
        return self.stats


def main() -> None:
    conninfo = os.environ.get("CANTINA_DB", "dbname=cantina_demo")
    with psycopg.connect(conninfo) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        conn.execute(cast(LiteralString, SCHEMA.read_text()))
        stats = Seeder(conn).run()
        # Recreating the schema dropped the per-school roles' grants and policies.
        from cantina_ai.demo.access import POLICY
        from cantina_ai.engine.tenant_guard import setup_sql

        for (school_id,) in conn.execute("SELECT id FROM schools ORDER BY id").fetchall():
            conn.commit()
            conn.execute(cast(LiteralString, setup_sql(POLICY, school_id)))
    for k, v in sorted(stats.items()):
        print(f"{k}: {v}")
