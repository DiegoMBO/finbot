-- Migração 004: salário (CLT), horas extras, 13º e férias.
-- Só CRIA tabelas novas; não altera nem apaga nada das existentes.

-- Vínculo de emprego (uma linha só): admissão e as datas em que a empresa paga o 13º.
CREATE TABLE employment (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    admission_date      TEXT    NOT NULL,                 -- AAAA-MM-DD
    -- Datas do 13º no ano, "DD/MM" separadas por vírgula. A ÚLTIMA é a 2ª parcela
    -- (com descontos); as anteriores pagam a 1ª parcela (metade, sem descontos).
    thirteenth_schedule TEXT    NOT NULL DEFAULT '30/11,20/12',
    created_at          TEXT    NOT NULL
);

-- Salário: cada reajuste é uma linha nova (o histórico fica).
CREATE TABLE salary_profiles (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_from      TEXT    NOT NULL UNIQUE,          -- AAAA-MM-DD
    gross_cents         INTEGER NOT NULL CHECK (gross_cents > 0),
    weekly_hours        INTEGER NOT NULL DEFAULT 44 CHECK (weekly_hours BETWEEN 1 AND 44),
    overtime_pct        INTEGER NOT NULL DEFAULT 50 CHECK (overtime_pct >= 50),
    holiday_pct         INTEGER NOT NULL DEFAULT 100 CHECK (holiday_pct >= 50),
    dependents          INTEGER NOT NULL DEFAULT 0 CHECK (dependents >= 0),
    payday_rule         TEXT    NOT NULL DEFAULT '5du',   -- '5du' (5º dia útil) ou 'dia:N'
    advance_pct         INTEGER CHECK (advance_pct BETWEEN 1 AND 99),  -- vale quinzenal
    advance_day         INTEGER CHECK (advance_day BETWEEN 1 AND 31),
    closing_day         INTEGER CHECK (closing_day BETWEEN 1 AND 31),  -- fechamento do ponto
    overtime_lag_months INTEGER NOT NULL DEFAULT 1 CHECK (overtime_lag_months BETWEEN 0 AND 3),
    created_at          TEXT    NOT NULL
);

-- Descontos do holerite. Exatamente UM destes: percent_bp (% do salário base,
-- 6% = 600), fixed_cents (valor fixo) ou debt_id (consignado descontado em folha:
-- o valor é a parcela da dívida, e o desconto vira pagamento dela).
CREATE TABLE salary_deductions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    percent_bp    INTEGER CHECK (percent_bp BETWEEN 1 AND 10000),
    fixed_cents   INTEGER CHECK (fixed_cents > 0),
    debt_id       INTEGER REFERENCES debts (id),
    created_at    TEXT    NOT NULL,
    CHECK ((percent_bp IS NOT NULL) + (fixed_cents IS NOT NULL) + (debt_id IS NOT NULL) = 1)
);

-- Horas extras registradas (o valor é calculado na hora, pelo salário vigente).
CREATE TABLE overtime_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    worked_on  TEXT    NOT NULL,                          -- AAAA-MM-DD
    minutes    INTEGER NOT NULL CHECK (minutes > 0),
    pct        INTEGER NOT NULL CHECK (pct >= 50),        -- adicional: 50, 100...
    note       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);

-- Férias tiradas ou agendadas.
CREATE TABLE vacations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    start_date         TEXT    NOT NULL,
    days               INTEGER NOT NULL CHECK (days BETWEEN 5 AND 30),
    sold_days          INTEGER NOT NULL DEFAULT 0 CHECK (sold_days BETWEEN 0 AND 10),
    thirteenth_advance INTEGER NOT NULL DEFAULT 0,        -- pediu a 1ª parcela do 13º junto?
    created_at         TEXT    NOT NULL
);

-- Pagamentos lançados pelo bot (salário, vale, 13º, férias): evita lançar duas
-- vezes e guarda o detalhamento. O /desfazer usa os ids dos lançamentos.
CREATE TABLE payroll_payments (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    kind               TEXT    NOT NULL CHECK (kind IN (
                           'salario', 'vale', '13-primeira', '13-segunda', 'ferias')),
    reference          TEXT    NOT NULL,                  -- AAAA-MM (competência) ou AAAA-MM-DD
    paid_on            TEXT    NOT NULL,
    gross_cents        INTEGER NOT NULL,
    inss_cents         INTEGER NOT NULL DEFAULT 0,
    irrf_cents         INTEGER NOT NULL DEFAULT 0,
    deductions_cents   INTEGER NOT NULL DEFAULT 0,        -- VT, plano... (sem consignado)
    net_cents          INTEGER NOT NULL,                  -- o que entrou como entrada
    transaction_id     INTEGER REFERENCES transactions (id),
    -- 'desfeito' = você desfez (/desfazer): o bot não lança de novo sozinho.
    status             TEXT    NOT NULL DEFAULT 'lancado' CHECK (status IN ('lancado', 'desfeito')),
    created_at         TEXT    NOT NULL,
    UNIQUE (kind, reference)
);

-- Os pagamentos de consignado lançados junto com um salário (para desfazer tudo junto).
CREATE TABLE payroll_links (
    payment_id     INTEGER NOT NULL REFERENCES payroll_payments (id),
    transaction_id INTEGER NOT NULL REFERENCES transactions (id),
    PRIMARY KEY (payment_id, transaction_id)
);
