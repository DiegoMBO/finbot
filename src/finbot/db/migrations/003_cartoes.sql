-- Migração 003: cartões de crédito como cadastro próprio (separado das dívidas).
-- Só CRIA tabelas novas; não altera nem apaga nada das tabelas existentes.

-- Um cartão. "Utilizado" = tudo o que ainda vai ser cobrado (fatura atual +
-- parcelas futuras). Disponível = limite − utilizado.
CREATE TABLE cards (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT    NOT NULL,
    name_key           TEXT    NOT NULL UNIQUE,          -- nome sem acento/maiúsculas, para busca
    closing_day        INTEGER NOT NULL CHECK (closing_day BETWEEN 1 AND 31),  -- "melhor dia de compra"
    due_day            INTEGER NOT NULL CHECK (due_day BETWEEN 1 AND 31),
    limit_cents        INTEGER NOT NULL CHECK (limit_cents > 0),
    used_cents         INTEGER NOT NULL DEFAULT 0 CHECK (used_cents >= 0),
    revolving_rate_ppm INTEGER NOT NULL DEFAULT 0 CHECK (revolving_rate_ppm >= 0),  -- rotativo, ao mês
    created_at         TEXT    NOT NULL                  -- UTC
);

-- Uma fatura por cartão e por mês de vencimento.
-- Total = soma dos itens, ou o valor informado pelo banco (/fatura), se houver.
CREATE TABLE invoices (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id              INTEGER NOT NULL REFERENCES cards (id),
    reference_month      TEXT    NOT NULL,                -- AAAA-MM do vencimento
    closing_date         TEXT    NOT NULL,                -- AAAA-MM-DD
    due_date             TEXT    NOT NULL,                -- AAAA-MM-DD
    informed_total_cents INTEGER CHECK (informed_total_cents >= 0),
    paid_cents           INTEGER NOT NULL DEFAULT 0 CHECK (paid_cents >= 0),
    rolled_at            TEXT,                            -- quando o que faltou foi para o rotativo
    UNIQUE (card_id, reference_month)
);

-- Cada compra feita no cartão (à vista = 1 parcela).
CREATE TABLE card_purchases (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id          INTEGER NOT NULL REFERENCES cards (id),
    description      TEXT    NOT NULL DEFAULT '',
    category         TEXT    NOT NULL,
    total_cents      INTEGER NOT NULL CHECK (total_cents > 0),   -- soma das parcelas
    installments     INTEGER NOT NULL CHECK (installments BETWEEN 1 AND 48),
    cash_price_cents INTEGER CHECK (cash_price_cents > 0),       -- preço à vista (juros embutidos)
    purchased_on     TEXT    NOT NULL,                           -- AAAA-MM-DD (data local)
    created_at       TEXT    NOT NULL                            -- UTC
);

-- O que compõe cada fatura. amount_cents é negativo no estorno (crédito).
CREATE TABLE invoice_items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id         INTEGER NOT NULL REFERENCES invoices (id),
    kind               TEXT    NOT NULL CHECK (kind IN ('anterior', 'compra', 'estorno', 'rotativo')),
    amount_cents       INTEGER NOT NULL,
    purchase_id        INTEGER REFERENCES card_purchases (id),
    installment_number INTEGER,                            -- 1, 2, 3... (compras parceladas)
    transaction_id     INTEGER REFERENCES transactions (id),  -- o gasto correspondente no /mes
    description        TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL
);

CREATE INDEX idx_invoice_items_invoice ON invoice_items (invoice_id);
CREATE INDEX idx_invoice_items_purchase ON invoice_items (purchase_id);
CREATE INDEX idx_invoice_items_transaction ON invoice_items (transaction_id);

-- Histórico do cartão. Linhas com o mesmo action_id são UMA ação sua
-- (ex.: um pagamento que abateu duas faturas): o /desfazer desfaz o grupo todo.
CREATE TABLE card_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id          INTEGER NOT NULL REFERENCES cards (id),
    action_id        INTEGER NOT NULL,
    kind             TEXT    NOT NULL CHECK (kind IN (
                         'cadastro', 'compra', 'pagamento', 'estorno', 'fatura', 'rotativo', 'ajuste')),
    amount_cents     INTEGER NOT NULL,
    used_before_cents INTEGER NOT NULL,
    used_after_cents INTEGER NOT NULL,
    purchase_id      INTEGER REFERENCES card_purchases (id),
    invoice_id       INTEGER REFERENCES invoices (id),
    occurred_at      TEXT    NOT NULL                     -- UTC
);

CREATE INDEX idx_card_events_card ON card_events (card_id, occurred_at);

-- Lembretes de vencimento de fatura já enviados.
CREATE TABLE card_reminders (
    card_id  INTEGER NOT NULL REFERENCES cards (id),
    due_date TEXT    NOT NULL,
    sent_at  TEXT    NOT NULL,
    PRIMARY KEY (card_id, due_date)
);
