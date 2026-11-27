-- Migração 005: saldo da conta informado por você (/saldo inicial).
-- Só CRIA uma tabela nova; não altera nem apaga nada das existentes.
-- Guarda o histórico: o /saldo usa sempre o mais recente.

CREATE TABLE cash_balances (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Pode ser negativo (conta no cheque especial). Em centavos.
    amount_cents INTEGER NOT NULL,
    informed_at  TEXT    NOT NULL   -- UTC: tudo registrado DEPOIS disso mexe no saldo
);
