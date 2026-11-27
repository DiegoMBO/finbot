-- Migração 002: módulo de dívidas.
-- Só CRIA tabelas novas; não altera nem apaga nada das tabelas existentes.
-- Sem "IF NOT EXISTS" de propósito: se a tabela já existisse, algo estaria
-- errado com a versão do banco, e é melhor falhar (com rollback) do que seguir.

-- Uma linha por dívida. Os campos que podem ser pulados no cadastro aceitam NULL.
CREATE TABLE debts (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    name                   TEXT    NOT NULL,           -- como você escreveu: "Empréstimo Nubank"
    name_key               TEXT    NOT NULL UNIQUE,    -- normalizado: "emprestimo nubank"
    kind                   TEXT    NOT NULL CHECK (kind IN (
                               'emprestimo', 'cartao', 'financiamento',
                               'cheque_especial', 'parcelamento', 'pessoa')),
    creditor               TEXT,
    balance_cents          INTEGER NOT NULL CHECK (balance_cents >= 0),  -- saldo devedor atual
    -- Taxa mensal em MILIONÉSIMOS (inteiro, pelo mesmo motivo dos centavos):
    -- 1,99% a.m. = 0,0199 = 19900. Precisão de 4 casas no percentual.
    monthly_rate_ppm       INTEGER NOT NULL DEFAULT 0 CHECK (monthly_rate_ppm >= 0),
    payment_cents          INTEGER CHECK (payment_cents > 0),            -- parcela/pagamento mensal
    due_day                INTEGER CHECK (due_day BETWEEN 1 AND 31),
    remaining_installments INTEGER CHECK (remaining_installments >= 0),
    start_date             TEXT,                       -- AAAA-MM-DD (data local)
    status                 TEXT    NOT NULL DEFAULT 'ativa' CHECK (status IN ('ativa', 'quitada')),
    created_at             TEXT    NOT NULL,           -- UTC, mesmo formato de transactions
    closed_at              TEXT
);

-- Cartão de crédito: uma fatura por mês de referência.
CREATE TABLE card_statements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    debt_id         INTEGER NOT NULL REFERENCES debts (id),
    reference_month TEXT    NOT NULL,                  -- AAAA-MM
    due_date        TEXT    NOT NULL,                  -- AAAA-MM-DD
    total_cents     INTEGER NOT NULL CHECK (total_cents >= 0),
    minimum_cents   INTEGER CHECK (minimum_cents >= 0),
    paid_cents      INTEGER NOT NULL DEFAULT 0 CHECK (paid_cents >= 0),
    UNIQUE (debt_id, reference_month)
);

-- Histórico: uma linha por cadastro, pagamento, ajuste manual ou fatura.
-- A evolução do saldo é a sequência de balance_after_cents ao longo do tempo.
CREATE TABLE debt_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    debt_id              INTEGER NOT NULL REFERENCES debts (id),
    kind                 TEXT    NOT NULL CHECK (kind IN ('cadastro', 'pagamento', 'ajuste', 'fatura')),
    amount_cents         INTEGER NOT NULL,             -- valor pago, ou diferença do ajuste
    interest_cents       INTEGER NOT NULL DEFAULT 0,   -- juros do mês lançados neste evento
    balance_before_cents INTEGER NOT NULL,
    balance_after_cents  INTEGER NOT NULL,
    transaction_id       INTEGER REFERENCES transactions (id),     -- o gasto em "dívidas"
    statement_id         INTEGER REFERENCES card_statements (id),
    occurred_at          TEXT    NOT NULL              -- UTC
);

CREATE INDEX idx_debt_events_debt ON debt_events (debt_id, occurred_at);
CREATE INDEX idx_debt_events_transaction ON debt_events (transaction_id);

-- Lembretes já enviados: evita aviso duplicado e permite enviar um atrasado
-- quando o bot estava desligado no dia certo.
CREATE TABLE debt_reminders (
    debt_id  INTEGER NOT NULL REFERENCES debts (id),
    due_date TEXT    NOT NULL,                         -- AAAA-MM-DD
    sent_at  TEXT    NOT NULL,                         -- UTC
    PRIMARY KEY (debt_id, due_date)
);
