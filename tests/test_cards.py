"""Testes do cartão de crédito: faturas, pagamentos por fatura e rotativo.

Contas à mão (rotativo de 15% a.m.):
- fatura R$ 1.850,40, mínimo R$ 277,56 -> pagando só o mínimo sobram
  1.850,40 − 277,56 = 1.572,84; juros de 1 mês = 1.572,84 × 0,15 = 235,926 -> R$ 235,93
- pagando R$ 1.250,40 sobram R$ 600,00; juros = 600 × 0,15 = R$ 90,00 -> vira R$ 690,00
- 15% a.m. ao ano: 1,15¹² − 1 = 435,025% (conta em test_finance)
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakes import Bot

from finbot import db
from finbot.bot import cards as card_texts
from finbot.bot import handlers
from finbot.parsing.card import CardArgsError, parse_statement_args, resolve_day_month
from finbot.services import cards, debts

SP = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 11, 26, 15, tzinfo=UTC)
TODAY = date(2026, 11, 26)
DEC_10 = date(2026, 12, 10)
CARD = debts.NewDebt("Cartão Nubank", "cartao", 1, creditor="Nubank", rate_ppm=150_000, due_day=10)


@pytest.fixture
def conn():
    c = db.connect(db.MEMORY)
    yield c
    c.close()


@pytest.fixture
def card(conn):
    return debts.create(conn, CARD, NOW)


def pay(conn, debt, cents):
    return debts.pay(conn, debts.get(conn, debt.id), cents, NOW, SP)


# --- faturas e pagamentos ---------------------------------------------------


def test_fatura_vira_o_saldo_do_cartao(conn, card):
    s = cards.register(conn, card.id, 185_040, 27_756, DEC_10, NOW)
    assert (s.reference_month, s.total_cents, s.minimum_cents, s.paid_cents) == (
        "2026-12",
        185_040,
        27_756,
        0,
    )
    assert debts.get(conn, card.id).balance_cents == 185_040
    assert debts.events(conn, card.id)[-1].kind == "fatura"


def test_pagamento_parcial_abate_a_fatura_e_sem_juros_no_pagamento(conn, card):
    cards.register(conn, card.id, 185_040, 27_756, DEC_10, NOW)
    r = pay(conn, card, 125_040)
    assert r.interest_cents == 0
    assert (r.statement.paid_cents, r.statement.remaining_cents) == (125_040, 60_000)
    assert r.balance_after_cents == 60_000
    assert not r.statement.fully_paid


def test_pagar_tudo_deixa_o_cartao_em_dia_mas_ativo(conn, card):
    cards.register(conn, card.id, 185_040, None, DEC_10, NOW)
    r = pay(conn, card, 185_040)
    assert r.statement.fully_paid
    assert (r.debt.balance_cents, r.debt.status) == (0, "ativa")
    assert [d.name for d in debts.list_active(conn)] == ["Cartão Nubank"]  # não "quitou"


def test_desfazer_pagamento_devolve_a_fatura(conn, card):
    cards.register(conn, card.id, 185_040, None, DEC_10, NOW)
    pay(conn, card, 125_040)
    r = pay(conn, card, 60_000)
    debts.undo_payment(conn, r.transaction_id, SP)
    s = cards.current(conn, card.id)
    assert (s.paid_cents, s.remaining_cents) == (125_040, 60_000)
    assert debts.get(conn, card.id).balance_cents == 60_000


def test_fatura_seguinte_substitui_o_saldo(conn, card):
    cards.register(conn, card.id, 185_040, None, DEC_10, NOW)
    pay(conn, card, 125_040)  # sobrou 600 no rotativo
    # A fatura de janeiro já vem com o rotativo somado pelo banco.
    s = cards.register(conn, card.id, 210_000, None, date(2027, 1, 10), NOW)
    assert s.reference_month == "2027-01"
    assert debts.get(conn, card.id).balance_cents == 210_000
    assert [h.reference_month for h in cards.history(conn, card.id)] == ["2026-12", "2027-01"]


def test_corrigir_fatura_do_mesmo_mes_mantem_o_que_foi_pago(conn, card):
    cards.register(conn, card.id, 185_040, None, DEC_10, NOW)
    pay(conn, card, 125_040)
    s = cards.register(conn, card.id, 190_000, None, DEC_10, NOW)
    assert (s.total_cents, s.paid_cents, s.remaining_cents) == (190_000, 125_040, 64_960)
    assert debts.get(conn, card.id).balance_cents == 64_960


def test_fatura_mais_antiga_que_a_atual_e_recusada(conn, card):
    cards.register(conn, card.id, 100_000, None, DEC_10, NOW)
    with pytest.raises(ValueError, match="mais nova"):
        cards.register(conn, card.id, 50_000, None, date(2026, 11, 10), NOW)


def test_minimo_maior_que_total_recusado(conn, card):
    with pytest.raises(ValueError, match="mínimo"):
        cards.register(conn, card.id, 10_000, 20_000, DEC_10, NOW)


def test_pagamento_de_cartao_sem_fatura_so_abate(conn):
    d = debts.create(conn, debts.NewDebt("Cartão Inter", "cartao", 50_000), NOW)
    r = pay(conn, d, 20_000)
    assert (r.statement, r.balance_after_cents) == (None, 30_000)


def test_abaixo_do_minimo(conn, card):
    cards.register(conn, card.id, 185_040, 27_756, DEC_10, NOW)
    assert pay(conn, card, 10_000).statement.below_minimum


def test_cheque_especial_zerado_continua_ativo(conn):
    d = debts.create(conn, debts.NewDebt("Cheque", "cheque_especial", 50_000, rate_ppm=80_000), NOW)
    updated, _ = debts.set_balance(conn, d, 0, NOW)
    assert updated.status == "ativa"


def test_custo_do_rotativo_feito_a_mao():
    c = cards.revolving_cost(60_000, 150_000)
    assert (c.interest_cents, c.annual_ppm) == (9_000, 4_350_250)
    assert cards.revolving_cost(157_284, 150_000).interest_cents == 23_593  # 235,926 -> 235,93


# --- textos -----------------------------------------------------------------


def _statement(paid=0, minimum=27_756, due=DEC_10):
    return cards.Statement(1, 1, f"{due:%Y-%m}", due, 185_040, minimum, paid)


def _card(rate=150_000):
    return debts.Debt(
        1, "Cartão Nubank", "cartao", "Nubank", 0, rate, None, 10, None, None, "ativa", NOW
    )


def test_alerta_de_rotativo_com_os_numeros():
    text = card_texts.revolving_warning(_card(), _statement(paid=125_040), TODAY)
    assert "ATENÇÃO: ROTATIVO" in text
    assert (
        "Faltam R$ 600,00 da fatura de dezembro/2026 (total R$ 1.850,40, pago R$ 1.250,40)" in text
    )
    assert "Se não pagar até 10/12" in text
    assert "15,00% ao mês (435,025% ao ano)" in text
    assert "Em 1 mês, R$ 600,00 viram R$ 690,00 (+R$ 90,00 de juros)" in text
    assert "abaixo do mínimo" not in text


def test_alerta_depois_do_vencimento_e_abaixo_do_minimo():
    text = card_texts.revolving_warning(_card(), _statement(paid=10_000), date(2026, 12, 15))
    assert "venceu em 10/12: esse valor já está no rotativo" in text
    assert "abaixo do mínimo (R$ 277,56)" in text


def test_alerta_sem_taxa_cadastrada():
    text = card_texts.revolving_warning(_card(rate=0), _statement(paid=100), TODAY)
    assert "Taxa do rotativo não cadastrada" in text


def test_fatura_paga_por_completo():
    text = card_texts.revolving_warning(_card(), _statement(paid=185_040), TODAY)
    assert text.startswith("✅ Fatura de dezembro/2026 paga por completo")


def test_situacao_da_fatura_mostra_o_custo_de_pagar_so_o_minimo():
    text = card_texts.format_statement(_card(), _statement(), TODAY)
    assert "Total: R$ 1.850,40 · mínimo: R$ 277,56 · vence 10/12/2026 (em 14 dias)" in text
    assert "Se pagar só o mínimo, R$ 1.572,84 vão para o rotativo" in text
    assert "(+R$ 235,93 de juros)" in text


@pytest.mark.parametrize(
    ("due", "expected"),
    [
        (date(2026, 11, 26), "(HOJE)"),
        (date(2026, 11, 27), "(amanhã)"),
        (date(2026, 11, 20), "venceu em 20/11/2026 (há 6 dia(s))"),
    ],
)
def test_texto_do_vencimento(due, expected):
    assert expected in card_texts.due_text(due, TODAY)


# --- argumentos do /fatura --------------------------------------------------


def test_argumentos_da_fatura():
    a = parse_statement_args(["nubank", "1.850,40", "277,56", "10/12"], TODAY)
    assert (a.name, a.total_cents, a.minimum_cents, a.due_date) == (
        "nubank",
        185_040,
        27_756,
        DEC_10,
    )
    a = parse_statement_args(
        ["cartão", "nubank", "1850,40", "mínimo", "277,56", "vence", "10/12/2026"], TODAY
    )
    assert (a.name, a.minimum_cents, a.due_date) == ("cartão nubank", 27_756, DEC_10)
    a = parse_statement_args(["nubank"], TODAY)
    assert (a.total_cents, a.due_date) == (None, None)


@pytest.mark.parametrize(
    "args",
    [
        ["1.850"],
        ["nubank", "100", "200"],
        ["nubank", "1", "2", "3"],
        ["nubank", "100", "10/06/2026"],
    ],
)
def test_argumentos_invalidos(args):
    with pytest.raises(CardArgsError):
        parse_statement_args(args, TODAY)


def test_ano_do_vencimento_sem_ano():
    assert resolve_day_month(10, 12, TODAY) == date(2026, 12, 10)
    assert resolve_day_month(5, 1, TODAY) == date(2027, 1, 5)  # virada de ano
    assert resolve_day_month(20, 11, TODAY) == date(2026, 11, 20)


# --- pelo "chat" ------------------------------------------------------------


def test_fatura_e_pagamento_parcial_pelo_chat():
    bot = Bot()
    debts.create(bot.conn, CARD, NOW)
    due = (datetime.now(SP).date() + timedelta(days=10)).strftime("%d/%m/%Y")

    bot.send(card_texts.statement_command, args=["nubank", "1.850,40", "277,56", due])
    assert "Registrada." in bot.last
    assert "Se pagar só o mínimo, R$ 1.572,84 vão para o rotativo" in bot.last

    bot.send(handlers.register, "paguei 1250,40 da fatura")
    assert "Pagamento de R$ 1.250,40 em Cartão Nubank" in bot.last
    assert "ATENÇÃO: ROTATIVO" in bot.last
    assert "R$ 600,00 viram R$ 690,00" in bot.last

    bot.send(card_texts.statement_command, args=["nubank"])
    assert "Pago: R$ 1.250,40 · falta: R$ 600,00" in bot.last

    bot.send(handlers.register, "paguei 600 do cartão nubank")
    assert "paga por completo" in bot.last
    bot.close()


def test_fatura_sem_vencimento_cadastrado_pede_a_data():
    bot = Bot()
    debts.create(bot.conn, debts.NewDebt("Cartão Inter", "cartao", 1), NOW)
    bot.send(card_texts.statement_command, args=["inter", "500"])
    assert "informe a data" in bot.last
    bot.send(card_texts.statement_command, args=["carro", "500"])
    assert "Não encontrei o cartão" in bot.last
    bot.close()
