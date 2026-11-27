-- Demo database: a school cafeteria with prepaid balances (fictional and simplified).
--
-- Inspired by CantinaPro, a real cafeteria payments product, but NOT its schema.
-- A few real-world traps are kept on purpose, because they are what makes
-- text-to-SQL hard (see docs/rules.md and the case study):
--   * a transaction has a header (transactions) and lines (transaction_items):
--     joining them and summing the header amount counts it once per line;
--   * `cancelled` may be NULL (meaning false);
--   * a purchase paid in cash at the counter is stored as a purchase that does not
--     touch the balance PLUS a cash top-up that pays for it (pays_transaction_id);
--   * lunch is charged even when the balance is not enough (a documented rule);
--   * a refund is a new transaction, the purchase only gets cancelled = true;
--   * cash withdrawals and the end-of-day close are both kind = 'withdrawal'.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE schools (
    id                  int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                text NOT NULL,
    two_step_orders     boolean NOT NULL DEFAULT false, -- order placed, then handed over
    daily_limit_enabled boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE operators (                 -- cashiers
    id        int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id int NOT NULL REFERENCES schools,
    name      text NOT NULL
);

CREATE TABLE guardians (
    id        int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id int NOT NULL REFERENCES schools,
    name      text NOT NULL
);

CREATE TABLE students (
    id          int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id   int NOT NULL REFERENCES schools,
    name        text NOT NULL,
    grade       text NOT NULL,                -- class, e.g. '3º ano A'
    balance     numeric(10,2) NOT NULL DEFAULT 0, -- prepaid balance, may go negative
    daily_limit numeric(10,2),                -- NULL = no limit
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE student_guardians (
    student_id  int NOT NULL REFERENCES students,
    guardian_id int NOT NULL REFERENCES guardians,
    PRIMARY KEY (student_id, guardian_id)
);

CREATE TABLE categories (
    id        int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id int NOT NULL REFERENCES schools,
    name      text NOT NULL
);

CREATE TABLE products (
    id             int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id      int NOT NULL REFERENCES schools,
    category_id    int NOT NULL REFERENCES categories,
    name           text NOT NULL,
    stock          int NOT NULL DEFAULT 0,
    variable_price boolean NOT NULL DEFAULT false, -- price typed at the counter (lunch)
    is_lunch       boolean NOT NULL DEFAULT false,
    active         boolean NOT NULL DEFAULT true
);

CREATE TABLE combos (                    -- one product from each slot category
    id        int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id int NOT NULL REFERENCES schools,
    name      text NOT NULL
);

CREATE TABLE combo_slots (
    combo_id    int NOT NULL REFERENCES combos,
    category_id int NOT NULL REFERENCES categories,
    PRIMARY KEY (combo_id, category_id)
);

-- Prices live in menus; only the active menu of a school applies.
CREATE TABLE menus (
    id         int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id  int NOT NULL REFERENCES schools,
    name       text NOT NULL,
    active     boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE menu_prices (
    menu_id    int NOT NULL REFERENCES menus,
    product_id int REFERENCES products,
    combo_id   int REFERENCES combos,
    price      numeric(10,2) NOT NULL,
    CHECK ((product_id IS NULL) <> (combo_id IS NULL))
);

CREATE TABLE cash_registers (
    id          int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id   int NOT NULL REFERENCES schools,
    operator_id int NOT NULL REFERENCES operators,
    status      text NOT NULL CHECK (status IN ('open', 'closed')),
    balance     numeric(10,2) NOT NULL DEFAULT 0, -- cash and PIX received, minus withdrawals
    opened_at   timestamptz NOT NULL,
    closed_at   timestamptz
);

CREATE TABLE transactions (
    id                  int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id           int NOT NULL REFERENCES schools,
    -- purchase     a student buys with the balance (or pays in cash, see pays_transaction_id)
    -- counter_sale a sale without a student (teachers, visitors), paid at the counter
    -- top_up       money into a student's balance (payment_method pix | cash)
    -- refund       balance given back for a cancelled purchase
    -- withdrawal   money taken out of a cash register (mid-day, or the end-of-day close)
    kind                text NOT NULL
                        CHECK (kind IN ('purchase', 'counter_sale', 'top_up', 'refund', 'withdrawal')),
    student_id          int REFERENCES students,
    operator_id         int REFERENCES operators,
    cash_register_id    int REFERENCES cash_registers,
    amount              numeric(10,2) NOT NULL,      -- header total (sum of its items for sales)
    payment_method      text CHECK (payment_method IN ('pix', 'cash')),
    is_lunch            boolean NOT NULL DEFAULT false,
    -- a cash top-up that pays for a purchase at the counter: that purchase did not
    -- debit the balance, and this top-up did not credit it
    pays_transaction_id int REFERENCES transactions,
    refund_of_id        int REFERENCES transactions,
    cancelled           boolean,                      -- NULL means false
    cancelled_at        timestamptz,
    created_at          timestamptz NOT NULL
);

CREATE TABLE transaction_items (
    id             int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id int NOT NULL REFERENCES transactions,
    product_id     int REFERENCES products,
    combo_id       int REFERENCES combos,
    amount         numeric(10,2) NOT NULL,
    CHECK ((product_id IS NULL) <> (combo_id IS NULL))
);

-- The products chosen inside a combo line.
CREATE TABLE item_components (
    item_id    int NOT NULL REFERENCES transaction_items,
    product_id int NOT NULL REFERENCES products
);

CREATE TABLE orders (                    -- fulfilment of a sale at the counter
    id             int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id      int NOT NULL REFERENCES schools,
    transaction_id int NOT NULL REFERENCES transactions,
    status         text NOT NULL CHECK (status IN ('waiting', 'delivered', 'cancelled')),
    created_at     timestamptz NOT NULL,
    delivered_at   timestamptz
);

CREATE TABLE stock_entries (
    id         int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    school_id  int NOT NULL REFERENCES schools,
    product_id int NOT NULL REFERENCES products,
    quantity   int NOT NULL,
    unit_cost  numeric(10,2) NOT NULL,
    supplier   text NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE INDEX ON transactions (school_id, created_at);
CREATE INDEX ON transactions (student_id, created_at);
CREATE INDEX ON transaction_items (transaction_id);
-- Used by the row-level security of the child tables (an EXISTS on the parent) and by
-- the joins of the stock and statement tools.
CREATE INDEX ON item_components (item_id);
CREATE INDEX ON orders (transaction_id);
CREATE INDEX ON transactions (pays_transaction_id) WHERE pays_transaction_id IS NOT NULL;
