"""Testes das migrações: os dados existentes NUNCA podem se perder."""

import sqlite3

import pytest

from finbot import db

# Esquema exatamente como era antes do sistema de migrações (user_version = 0).
LEGACY_SCHEMA = """
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('expense', 'income')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    category TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_transactions_created_at ON transactions (created_at);
CREATE TABLE budgets (
    category TEXT PRIMARY KEY,
    limit_cents INTEGER NOT NULL CHECK (limit_cents > 0)
);
INSERT INTO transactions (kind, amount_cents, category, description, created_at)
VALUES ('expense', 3500, 'alimentação', 'mercado', '2026-11-26T15:00:00Z');
INSERT INTO budgets VALUES ('lazer', 20000);
"""

NEW_TABLES = {"debts", "debt_events", "card_statements", "debt_reminders"}


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


@pytest.fixture
def legacy_db(tmp_path):
    path = tmp_path / "finbot.db"
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.close()
    return path


def test_migracoes_em_sequencia():
    versions = [m.version for m in db.load_migrations()]
    assert versions == list(range(1, len(versions) + 1))
    assert len(versions) >= 2


def test_banco_novo_fica_na_ultima_versao_sem_backup(tmp_path):
    path = tmp_path / "novo.db"
    conn = db.connect(path)
    assert db.schema_version(conn) == len(db.load_migrations())
    assert {"transactions", "budgets"} | NEW_TABLES <= tables(conn)
    conn.close()
    assert not (tmp_path / "backups").exists()  # nada a perder, nada a copiar


def test_banco_antigo_preserva_dados_e_faz_backup(legacy_db):
    conn = db.connect(legacy_db)
    assert db.schema_version(conn) == len(db.load_migrations())
    assert NEW_TABLES <= tables(conn)
    row = conn.execute("SELECT * FROM transactions").fetchone()
    assert (row["amount_cents"], row["category"], row["description"]) == (
        3500,
        "alimentação",
        "mercado",
    )
    assert conn.execute("SELECT * FROM budgets").fetchone()["limit_cents"] == 20000
    conn.close()

    backups = list((legacy_db.parent / "backups").glob("finbot-*-v0.db"))
    assert len(backups) == 1
    # O backup é uma cópia fiel do banco ANTES da migração.
    copy = sqlite3.connect(backups[0])
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 0
    assert copy.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert "debts" not in tables(copy)
    copy.close()


def test_reabrir_nao_migra_nem_copia_de_novo(legacy_db):
    db.connect(legacy_db).close()
    db.connect(legacy_db).close()
    assert len(list((legacy_db.parent / "backups").iterdir())) == 1


def test_migracao_com_erro_desfaz_tudo(legacy_db):
    conn = sqlite3.connect(legacy_db)
    migrations = [
        db.Migration(1, "001_ok.sql", "CREATE TABLE IF NOT EXISTS budgets (category TEXT);"),
        db.Migration(2, "002_quebrada.sql", "CREATE TABLE nova (x INTEGER); ISSO NAO E SQL;"),
    ]
    with pytest.raises(db.MigrationError, match="002_quebrada"):
        db.migrate(conn, migrations, legacy_db)
    # A 001 entrou; a 002 foi desfeita inteira (a tabela "nova" não ficou pela metade).
    assert db.schema_version(conn) == 1
    assert "nova" not in tables(conn)
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    conn.close()


def test_chaves_estrangeiras_ligadas():
    conn = db.connect(db.MEMORY)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO debt_events (debt_id, kind, amount_cents, balance_before_cents,"
            " balance_after_cents, occurred_at) VALUES (999, 'pagamento', 1, 1, 0, 'x')"
        )
    conn.close()
