"""Testes do cadastro e da consulta de dívidas (banco em memória)."""

from datetime import UTC, date, datetime

import pytest

from finbot import db
from finbot.services import debts

NOW = datetime(2026, 11, 26, 15, tzinfo=UTC)
NUBANK = debts.NewDebt(
    name="Empréstimo Nubank",
    kind="emprestimo",
    balance_cents=500_000,
    creditor="Nubank",
    rate_ppm=30_000,
    payment_cents=30_000,
    due_day=10,
    remaining_installments=24,
    start_date=date(2025, 3, 15),
)


@pytest.fixture
def conn():
    c = db.connect(db.MEMORY)
    yield c
    c.close()


def test_cadastro_grava_tudo_e_cria_primeiro_ponto_do_historico(conn):
    d = debts.create(conn, NUBANK, NOW)
    assert (d.name, d.kind_label, d.balance_cents, d.rate_ppm) == (
        "Empréstimo Nubank",
        "empréstimo",
        500_000,
        30_000,
    )
    assert (d.due_day, d.remaining_installments, d.start_date) == (10, 24, date(2025, 3, 15))
    assert d.status == "ativa"
    [event] = debts.events(conn, d.id)
    assert (event.kind, event.balance_before_cents, event.balance_after_cents) == (
        "cadastro",
        0,
        500_000,
    )


def test_campos_opcionais_vazios(conn):
    d = debts.create(conn, debts.NewDebt("Tia Rosa", "pessoa", 80_000), NOW)
    assert (d.creditor, d.rate_ppm, d.payment_cents, d.due_day, d.start_date) == (
        None,
        0,
        None,
        None,
        None,
    )


def test_nome_repetido_ignora_acento_e_maiuscula(conn):
    debts.create(conn, NUBANK, NOW)
    assert debts.name_taken(conn, "emprestimo  NUBANK")
    with pytest.raises(debts.DuplicateDebtError):
        debts.create(conn, debts.NewDebt("EMPRESTIMO nubank", "emprestimo", 100), NOW)


def test_saldo_zero_recusado(conn):
    with pytest.raises(ValueError):
        debts.create(conn, debts.NewDebt("X", "pessoa", 0), NOW)


def test_busca(conn):
    debts.create(conn, NUBANK, NOW)
    debts.create(conn, debts.NewDebt("Cartão Nubank", "cartao", 100_000, creditor="Nubank"), NOW)
    debts.create(conn, debts.NewDebt("Carro", "financiamento", 2_000_000, creditor="Banco X"), NOW)

    assert [d.name for d in debts.find(conn, "carro")] == ["Carro"]  # nome exato
    assert [d.name for d in debts.find(conn, "emprestimo")] == ["Empréstimo Nubank"]
    assert [d.name for d in debts.find(conn, "banco x")] == ["Carro"]  # pelo credor
    assert [d.name for d in debts.find(conn, "cart nu")] == ["Cartão Nubank"]  # começo das palavras
    assert len(debts.find(conn, "nubank")) == 2  # ambíguo: quem chama decide
    assert debts.find(conn, "itau") == []
    assert debts.find(conn, "  ") == []


def test_lista_ordena_pela_maior_taxa(conn):
    debts.create(conn, debts.NewDebt("Barata", "emprestimo", 100, rate_ppm=10_000), NOW)
    debts.create(conn, debts.NewDebt("Cara", "cheque_especial", 100, rate_ppm=80_000), NOW)
    assert [d.name for d in debts.list_active(conn)] == ["Cara", "Barata"]


def test_contrato_divergente(conn):
    # Saldo 5.000 a 3% pagando 300 = 24 parcelas (calculado à mão em test_finance).
    ok = debts.create(conn, NUBANK, NOW)
    assert debts.contract_mismatch(ok) is None
    wrong = debts.create(
        conn,
        debts.NewDebt(**{**NUBANK.__dict__, "name": "Outro", "remaining_installments": 12}),
        NOW,
    )
    assert debts.contract_mismatch(wrong) == 24
