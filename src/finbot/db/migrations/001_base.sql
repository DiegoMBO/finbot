-- Migração 001: esquema original (gastos, entradas e orçamentos).
-- Usa "IF NOT EXISTS" porque bancos criados antes do sistema de migrações
-- já têm essas tabelas: nesse caso esta migração não muda nada, só marca a versão 1.

-- Cada gasto ou entrada é uma linha.
-- Dinheiro é guardado em CENTAVOS (inteiro): R$ 12,50 vira 1250. Com float,
-- 0.1 + 0.2 dá 0.30000000000000004; com inteiros a conta é sempre exata.
CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL CHECK (kind IN ('expense', 'income')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    category     TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    -- Data/hora em UTC, formato fixo "AAAA-MM-DDTHH:MM:SSZ". Com formato fixo,
    -- comparar textos equivale a comparar datas (dá para usar BETWEEN e ORDER BY).
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON transactions (created_at);

-- Limite mensal por categoria (usado pelo /orcamento).
CREATE TABLE IF NOT EXISTS budgets (
    category    TEXT    PRIMARY KEY,
    limit_cents INTEGER NOT NULL CHECK (limit_cents > 0)
);
