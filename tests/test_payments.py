"""Testes de pagamento, ajuste de saldo e desfazer, conferidos com contas à mão."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from finbot import db
from finbot.parsing.message import ParsedEntry
from finbot.parsing.payment import (
    PaymentArgsError,
    parse_name_and_amount,
    parse_name_and_balance,
    parse_payment_intent,
)
from finbot.services import debts, transactions

SP = ZoneInfo("America/Sao_Paulo")
NOV_26 = datetime(2026, 11, 26, 15, tzinfo=UTC)
NOV_30_NIGHT = datetime(2026, 12, 1, 1, tzinfo=UTC)  # 30/11 às 22h em São Paulo
DEC_10 = datetime(2026, 12, 10, 12, tzinfo=UTC)

LOAN = debts.NewDebt(
    "Empréstimo Nubank",
    "emprestimo",
    500_000,
    creditor="Nubank",
    rate_ppm=30_000,
    payment_cents=30_000,
    due_day=10,
    remaining_installments=24,
)


@pytest.fixture
def conn():
    c = db.connect(db.MEMORY)
    yield c
    c.close()


def pay(conn, debt, cents, when):
    return debts.pay(conn, debts.get(conn, debt.id), cents, when, SP)


# --- pagamento --------------------------------------------------------------


def test_sequencia_de_pagamentos_feita_a_mao(conn):
    d = debts.create(conn, LOAN, NOV_26)

    # 1º do mês: juros 5.000 × 3% = 150,00 -> 5.000 + 150 − 300 = 4.850,00
    r = pay(conn, d, 30_000, NOV_26)
    assert (r.interest_cents, r.amortization_cents, r.balance_after_cents) == (
        15_000,
        15_000,
        485_000,
    )
    assert r.debt.remaining_installments == 23

    # 2º de novembro (30/11 22h em SP, já 01/12 em UTC): sem juros -> 4.850 − 100 = 4.750,00
    r = pay(conn, d, 10_000, NOV_30_NIGHT)
    assert (r.interest_cents, r.balance_after_cents) == (0, 475_000)
    assert r.debt.remaining_installments == 23  # pagamento extra não conta como parcela

    # 1º de dezembro: juros 4.750 × 3% = 142,50 -> 4.750 + 142,50 − 300 = 4.592,50
    r = pay(conn, d, 30_000, DEC_10)
    assert (r.interest_cents, r.balance_after_cents) == (14_250, 459_250)
    assert r.debt.remaining_installments == 22

    # Histórico: cadastro + 3 pagamentos, cada um ligado ao seu gasto.
    history = debts.events(conn, d.id)
    assert [e.kind for e in history] == ["cadastro", "pagamento", "pagamento", "pagamento"]
    assert [e.balance_after_cents for e in history] == [500_000, 485_000, 475_000, 459_250]

    # Os pagamentos viraram gastos na categoria "dívidas".
    spent = [t for t in transactions.list_all(conn)]
    assert [(t.kind, t.category, t.amount_cents) for t in spent] == [
        ("expense", "dívidas", 30_000),
        ("expense", "dívidas", 10_000),
        ("expense", "dívidas", 30_000),
    ]
    assert spent[0].description == "pagamento Empréstimo Nubank"


def test_pagamento_menor_que_os_juros_aumenta_o_saldo(conn):
    # 1.000 a 10%: juros 100; pagando 50 -> 1.000 + 100 − 50 = 1.050
    d = debts.create(
        conn, debts.NewDebt("Cheque", "cheque_especial", 100_000, rate_ppm=100_000), NOV_26
    )
    r = pay(conn, d, 5_000, NOV_26)
    assert (r.balance_after_cents, r.amortization_cents) == (105_000, -5_000)


def test_quitar_com_valor_acima_do_saldo(conn):
    d = debts.create(conn, debts.NewDebt("Tia", "pessoa", 10_000), NOV_26)
    r = pay(conn, d, 12_000, NOV_26)
    assert r.paid_off
    assert (r.balance_after_cents, r.overpaid_cents, r.amortization_cents) == (0, 2_000, 10_000)
    assert transactions.list_all(conn)[0].amount_cents == 12_000  # o gasto é o valor pago
    assert debts.list_active(conn) == []
    with pytest.raises(ValueError, match="quitada"):
        debts.pay(conn, debts.get(conn, d.id), 100, NOV_26, SP)


def test_cartao_nao_lanca_juros_no_pagamento(conn):
    d = debts.create(conn, debts.NewDebt("Cartão", "cartao", 150_000, rate_ppm=150_000), NOV_26)
    r = pay(conn, d, 50_000, NOV_26)
    assert (r.interest_cents, r.balance_after_cents) == (0, 100_000)


def test_pagamento_invalido(conn):
    d = debts.create(conn, LOAN, NOV_26)
    with pytest.raises(ValueError):
        debts.pay(conn, d, 0, NOV_26, SP)


# --- ajuste manual ----------------------------------------------------------


def test_ajuste_de_saldo(conn):
    d = debts.create(conn, LOAN, NOV_26)
    pay(conn, d, 30_000, NOV_26)  # 4.850,00
    updated, diff = debts.set_balance(conn, debts.get(conn, d.id), 490_000, NOV_26)
    assert (updated.balance_cents, diff) == (490_000, 5_000)
    last = debts.events(conn, d.id)[-1]
    assert (last.kind, last.amount_cents, last.balance_before_cents, last.balance_after_cents) == (
        "ajuste",
        5_000,
        485_000,
        490_000,
    )
    assert transactions.list_all(conn)[-1].amount_cents == 30_000  # ajuste não cria gasto


def test_ajuste_para_zero_quita(conn):
    d = debts.create(conn, LOAN, NOV_26)
    updated, _ = debts.set_balance(conn, d, 0, NOV_26)
    assert updated.status == "quitada"


# --- desfazer ---------------------------------------------------------------


def test_desfazer_pagamento_volta_saldo_parcelas_e_apaga_o_gasto(conn):
    d = debts.create(conn, LOAN, NOV_26)
    r = pay(conn, d, 30_000, NOV_26)
    debt, event = debts.undo_payment(conn, r.transaction_id, SP)
    assert (debt.balance_cents, debt.remaining_installments) == (500_000, 24)
    assert event.amount_cents == 30_000
    assert transactions.list_all(conn) == []
    assert [e.kind for e in debts.events(conn, d.id)] == ["cadastro"]


def test_desfazer_pagamento_extra_nao_mexe_nas_parcelas(conn):
    d = debts.create(conn, LOAN, NOV_26)
    pay(conn, d, 30_000, NOV_26)
    r = pay(conn, d, 10_000, NOV_30_NIGHT)
    debt, _ = debts.undo_payment(conn, r.transaction_id, SP)
    assert (debt.balance_cents, debt.remaining_installments) == (485_000, 23)


def test_desfazer_quitacao_reativa_a_divida(conn):
    # 600 sem juros, parcela 300, faltam 2: paga 300 (sobra 300, falta 1), paga 300 (quita)
    d = debts.create(
        conn,
        debts.NewDebt(
            "Loja", "parcelamento", 60_000, payment_cents=30_000, remaining_installments=2
        ),
        NOV_26,
    )
    pay(conn, d, 30_000, NOV_26)
    r = pay(conn, d, 30_000, DEC_10)
    assert r.paid_off and r.debt.remaining_installments == 0
    debt, _ = debts.undo_payment(conn, r.transaction_id, SP)
    assert (debt.status, debt.balance_cents, debt.remaining_installments) == ("ativa", 30_000, 1)


def test_desfazer_bloqueado_depois_de_ajuste(conn):
    d = debts.create(conn, LOAN, NOV_26)
    r = pay(conn, d, 30_000, NOV_26)
    debts.set_balance(conn, debts.get(conn, d.id), 490_000, NOV_26)
    with pytest.raises(debts.UndoBlockedError):
        debts.undo_payment(conn, r.transaction_id, SP)
    assert debts.get(conn, d.id).balance_cents == 490_000  # nada mudou


def test_desfazer_gasto_comum_nao_e_pagamento(conn):
    t = transactions.add(conn, ParsedEntry("expense", 100, "outros", ""), NOV_26)
    assert debts.undo_payment(conn, t.id, SP) is None


# --- reconhecer pagamento no texto ------------------------------------------


def test_intencao_de_pagamento():
    intent = parse_payment_intent("Paguei R$ 450,00 do Empréstimo Nubank!")
    assert (intent.amount_cents, intent.words) == (45_000, ["emprestimo", "nubank"])
    assert parse_payment_intent("paguei 300 da parcela do carro").words == ["carro"]
    assert parse_payment_intent("gastei 50 no mercado") is None  # "gastei" não é pagar dívida
    assert parse_payment_intent("paguei a conta") is None  # sem valor


@pytest.fixture
def several(conn):
    debts.create(conn, LOAN, NOV_26)
    debts.create(conn, debts.NewDebt("Cartão Nubank", "cartao", 100_000, creditor="Nubank"), NOV_26)
    debts.create(
        conn, debts.NewDebt("Carro", "financiamento", 2_000_000, creditor="Banco X"), NOV_26
    )
    debts.create(conn, debts.NewDebt("Tia Rosa", "pessoa", 50_000), NOV_26)
    return conn


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("paguei 450 do empréstimo nubank", ["Empréstimo Nubank"]),  # pelo nome
        ("paguei 200 nubank", ["Empréstimo Nubank", "Cartão Nubank"]),  # credor: ambíguo
        ("paguei 100 da fatura", ["Cartão Nubank"]),  # pelo tipo
        ("paguei 50 pra tia rosa", ["Tia Rosa"]),
        ("paguei 20 com o chaveiro", []),  # nada: segue como gasto comum
    ],
)
def test_qual_divida(several, text, expected):
    words = parse_payment_intent(text).words
    assert sorted(d.name for d in debts.match_payment(several, words)) == sorted(expected)


def test_palavra_que_sobra_gera_duvida(several):
    words = parse_payment_intent("paguei 100 de gasolina do carro").words
    [carro] = debts.match_payment(several, words)
    assert debts.unexplained_words(carro, words) == ["gasolina"]


def test_argumentos_pagar_e_saldo():
    assert parse_name_and_amount(["nubank", "450"]) == ("nubank", 45_000)
    assert parse_name_and_amount(["empréstimo", "nubank", "R$", "450,50"]) == (
        "empréstimo nubank",
        45_050,
    )
    assert parse_name_and_balance(["nubank", "4.812,37"]) == ("nubank", 481_237)
    assert parse_name_and_balance(["nubank", "0"]) == ("nubank", 0)
    for bad in (["nubank"], ["450"], ["nubank", "muito"]):
        with pytest.raises(PaymentArgsError):
            parse_name_and_amount(bad)
