"""Testes do serviço de transações, usando um banco SQLite em memória."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from finbot import db
from finbot.parsing.message import ParsedEntry
from finbot.services import transactions as tx

SP = ZoneInfo("America/Sao_Paulo")
MERCADO = ParsedEntry("expense", 3500, "alimentação", "mercado")
UBER = ParsedEntry("expense", 1250, "transporte", "uber")
SALARIO = ParsedEntry("income", 300000, "salário", "salário")


@pytest.fixture
def conn():
    c = db.connect(db.MEMORY)
    yield c
    c.close()


def test_add_devolve_registro_com_id_e_data_utc(conn):
    now = datetime(2026, 11, 26, 15, 30, tzinfo=UTC)
    t = tx.add(conn, MERCADO, now)
    assert t.id == 1
    assert t.created_at == now
    assert (t.kind, t.amount_cents, t.category) == ("expense", 3500, "alimentação")


def test_add_recusa_datetime_sem_fuso(conn):
    with pytest.raises(ValueError, match="fuso"):
        tx.add(conn, MERCADO, datetime(2026, 11, 26, 15, 30))


def test_dia_respeita_fuso_do_usuario(conn):
    # 23:30 em São Paulo no dia 26 = 02:30 UTC do dia 27. Tem que contar no dia 26.
    tx.add(conn, MERCADO, datetime(2026, 11, 27, 2, 30, tzinfo=UTC))
    # 00:30 em São Paulo do dia 27 = 03:30 UTC: já é o dia seguinte.
    tx.add(conn, UBER, datetime(2026, 11, 27, 3, 30, tzinfo=UTC))

    start, end = tx.day_bounds(date(2026, 11, 26), SP)
    assert [t.description for t in tx.list_between(conn, start, end)] == ["mercado"]
    start, end = tx.day_bounds(date(2026, 11, 27), SP)
    assert [t.description for t in tx.list_between(conn, start, end)] == ["uber"]


def test_delete_last_apaga_o_mais_recente(conn):
    now = datetime(2026, 11, 26, 12, tzinfo=UTC)
    tx.add(conn, MERCADO, now)
    tx.add(conn, UBER, now)  # mesma hora: o desempate é pelo id
    assert tx.delete_last(conn).description == "uber"
    assert tx.delete_last(conn).description == "mercado"
    assert tx.delete_last(conn) is None


def test_summarize():
    now = datetime(2026, 11, 26, tzinfo=UTC)
    items = [
        tx.Transaction(1, "expense", 1250, "transporte", "", now),
        tx.Transaction(2, "expense", 3500, "alimentação", "", now),
        tx.Transaction(3, "expense", 1000, "transporte", "", now),
        tx.Transaction(4, "income", 300000, "salário", "", now),
    ]
    s = tx.summarize(items)
    assert s.expense_cents == 5750
    assert s.income_cents == 300000
    assert s.balance_cents == 294250
    assert list(s.expenses_by_category.items()) == [("alimentação", 3500), ("transporte", 2250)]


def test_banco_em_arquivo_cria_pasta_e_persiste(tmp_path):
    path = tmp_path / "sub" / "finbot.db"
    c = db.connect(path)
    tx.add(c, MERCADO, datetime(2026, 11, 26, tzinfo=UTC))
    c.close()

    c = db.connect(path)  # reabrir não apaga nada (CREATE ... IF NOT EXISTS)
    assert tx.delete_last(c).description == "mercado"
    c.close()
