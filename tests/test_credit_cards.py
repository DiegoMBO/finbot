"""Serviço de cartões, conferido com um cenário feito à mão.

Hoje: 27/11/2026. Nubank: fecha dia 3, vence dia 10, limite 12.000,00,
utilizado 9.594,54, rotativo 14,5% a.m.
Fatura atual = a que vence 10/12/2026 (fecha 03/12): 1.840,00.
Próximas informadas: jan/2027 600,00 e fev/2027 400,00.
Sem mês definido: 9.594,54 − (1.840 + 600 + 400) = 6.754,54.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from finbot import db
from finbot.services import credit_cards as cc
from finbot.services import transactions

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2026, 11, 27)
NOW = datetime(2026, 11, 27, 15, tzinfo=UTC)  # 12h em São Paulo
NUBANK = cc.NewCard(
    name="Nubank",
    closing_day=3,
    due_day=10,
    limit_cents=1_200_000,
    used_cents=959_454,
    current_invoice_cents=184_000,
    next_invoices_cents=(60_000, 40_000),
    rate_ppm=145_000,
)


@pytest.fixture
def conn():
    c = db.connect(db.MEMORY)
    yield c
    c.close()


@pytest.fixture
def card(conn):
    return cc.create(conn, NUBANK, TODAY, NOW)


def invoice_totals(conn, card):
    return {i.reference_month: i.total_cents for i in cc.invoices(conn, card.id)}


def buy(conn, card, cents, n=1, desc="mercado", category="alimentação", on=TODAY, now=NOW):
    return cc.add_purchase(
        conn,
        cc.get(conn, card.id),
        description=desc,
        category=category,
        purchased_on=on,
        now=now,
        tz=SP,
        total_cents=cents,
        installments=n,
    )


# --- cadastro -----------------------------------------------------------------


def test_cadastro_cria_as_faturas_informadas(conn, card):
    assert invoice_totals(conn, card) == {"2026-12": 184_000, "2027-01": 60_000, "2027-02": 40_000}
    assert card.available_cents == 240_546  # 12.000 − 9.594,54
    assert card.usage_bp == 7_995  # 79,95%
    assert cc.unallocated_cents(conn, card) == 675_454
    current = cc.current_invoice(conn, card, TODAY)
    assert (current.due_date, current.closing_date) == (date(2026, 12, 10), date(2026, 12, 3))


def test_cadastro_invalido(conn, card):
    with pytest.raises(cc.DuplicateCardError):
        cc.create(conn, cc.NewCard("NUBANK", 3, 10, 100_000), TODAY, NOW)
    with pytest.raises(ValueError, match="somam mais"):
        cc.create(conn, cc.NewCard("Inter", 3, 10, 100_000, 10_000, 20_000), TODAY, NOW)


# --- compras ------------------------------------------------------------------


def test_compra_a_vista_entra_na_fatura_e_no_mes(conn, card):
    r = buy(conn, card, 8_000)
    assert [(a.invoice.reference_month, a.amount_cents) for a in r.allocations] == [
        ("2026-12", 8_000)
    ]
    assert invoice_totals(conn, card)["2026-12"] == 192_000  # 1.840 + 80
    assert r.card.used_cents == 967_454
    assert r.usage_alert == 80  # 79,95% -> 80,62%: cruzou 80%
    [t] = transactions.list_all(conn)
    assert (t.amount_cents, t.category, t.created_at) == (8_000, "alimentação", NOW)


def test_compra_parcelada_uma_parcela_por_fatura_e_por_mes(conn, card):
    r = buy(conn, card, 60_000, n=3, desc="tênis", category="compras")
    assert [(a.invoice.reference_month, a.amount_cents) for a in r.allocations] == [
        ("2026-12", 20_000),
        ("2027-01", 20_000),
        ("2027-02", 20_000),
    ]
    assert invoice_totals(conn, card) == {"2026-12": 204_000, "2027-01": 80_000, "2027-02": 60_000}
    assert r.card.used_cents == 1_019_454  # o limite é ocupado inteiro na hora
    # No /mes: uma parcela em cada mês, na data de vencimento da fatura.
    txs = transactions.list_all(conn)
    assert [(t.created_at.astimezone(SP).date(), t.description) for t in txs] == [
        (date(2026, 12, 10), "tênis (1/3)"),
        (date(2027, 1, 10), "tênis (2/3)"),
        (date(2027, 2, 10), "tênis (3/3)"),
    ]


def test_compra_no_dia_do_fechamento_vai_para_a_fatura_seguinte(conn, card):
    r = buy(conn, card, 5_000, on=date(2026, 12, 3), now=datetime(2026, 12, 3, 15, tzinfo=UTC))
    assert r.allocations[0].invoice.reference_month == "2027-01"


def test_parcelado_com_juros_embutidos(conn, card):
    r = cc.add_purchase(
        conn,
        card,
        description="tv",
        category="compras",
        purchased_on=TODAY,
        now=NOW,
        tz=SP,
        installments=10,
        installment_cents=33_000,
        cash_price_cents=300_000,
    )
    assert r.purchase.total_cents == 330_000
    assert r.usage_alert == 100  # 9.594,54 + 3.300 > 12.000: passou do limite
    assert r.card.available_cents < 0


# --- pagamento ----------------------------------------------------------------


def test_pagamento_abate_a_fatura_atual_e_libera_limite(conn, card):
    buy(conn, card, 8_000)  # dez = 1.920
    r = cc.pay(conn, cc.get(conn, card.id), 100_000, TODAY, NOW)
    assert [(a.invoice.reference_month, a.amount_cents) for a in r.allocations] == [
        ("2026-12", 100_000)
    ]
    assert r.allocations[0].invoice.remaining_cents == 92_000
    assert r.card.used_cents == 867_454  # 9.674,54 − 1.000
    assert transactions.list_all(conn)[-1].amount_cents == 8_000  # pagamento NÃO é gasto


def test_pagamento_grande_adianta_as_proximas(conn, card):
    # dez 1.840 + jan 600 + fev 400 = 2.840; pagando 3.000 sobram 160 fora das faturas
    r = cc.pay(conn, card, 300_000, TODAY, NOW)
    assert [(a.invoice.reference_month, a.amount_cents) for a in r.allocations] == [
        ("2026-12", 184_000),
        ("2027-01", 60_000),
        ("2027-02", 40_000),
    ]
    assert r.beyond_invoices_cents == 16_000
    assert r.card.used_cents == 659_454


# --- valor real da fatura -----------------------------------------------------


def test_fatura_informada_maior_sai_do_sem_mes_definido(conn, card):
    # O bot sabia de 1.840 em dezembro; o banco cobrou 1.870. Os 30 vêm das parcelas
    # antigas "sem mês definido" (6.754,54), então o utilizado não muda.
    inv, adjustment = cc.inform_invoice(conn, card, date(2026, 12, 10), 187_000, NOW)
    assert (inv.total_cents, adjustment) == (187_000, 0)
    assert cc.unallocated_cents(conn, cc.get(conn, card.id)) == 672_454


def test_fatura_informada_menor_reduz_o_utilizado(conn, card):
    inv, adjustment = cc.inform_invoice(conn, card, date(2027, 1, 10), 30_000, NOW)
    assert (inv.total_cents, adjustment) == (30_000, -30_000)
    assert cc.get(conn, card.id).used_cents == 929_454


# --- estorno ------------------------------------------------------------------


def test_estorno_total_antes_de_fechar_cancela_as_parcelas(conn, card):
    p = buy(conn, card, 60_000, n=3, desc="tênis").purchase
    r = cc.refund(conn, cc.get(conn, card.id), p, None, TODAY, NOW)
    assert (r.amount_cents, r.removed_future_cents, r.credit_cents) == (60_000, 60_000, 0)
    assert invoice_totals(conn, card) == {"2026-12": 184_000, "2027-01": 60_000, "2027-02": 40_000}
    assert transactions.list_all(conn) == []  # os gastos da compra saem do /mes
    assert r.card.used_cents == 959_454


def test_estorno_total_depois_de_fechar_vira_credito(conn, card):
    p = buy(conn, card, 60_000, n=3, desc="tênis").purchase
    # Em 05/12 a fatura de dezembro (fechou 03/12) já tem a 1ª parcela cobrada.
    r = cc.refund(conn, cc.get(conn, card.id), p, None, date(2026, 12, 5), NOW)
    assert (r.removed_future_cents, r.credit_cents) == (40_000, 20_000)  # jan e fev somem
    assert r.credit_invoice.reference_month == "2027-01"  # a fatura aberta em 05/12
    totals = invoice_totals(conn, card)
    assert totals["2026-12"] == 204_000  # a parcela de dez foi cobrada mesmo
    assert totals["2027-01"] == 40_000  # 600 − 200 de crédito
    assert totals["2027-02"] == 40_000


def test_estorno_parcial(conn, card):
    p = buy(conn, card, 8_000).purchase
    r = cc.refund(conn, cc.get(conn, card.id), p, 3_000, TODAY, NOW)
    assert (r.credit_cents, r.credit_invoice.reference_month) == (3_000, "2026-12")
    assert invoice_totals(conn, card)["2026-12"] == 189_000  # 1.840 + 80 − 30
    assert transactions.list_all(conn)[0].amount_cents == 5_000  # gasto de 80 virou 50
    with pytest.raises(ValueError):
        cc.refund(conn, cc.get(conn, card.id), p, 6_000, TODAY, NOW)  # só faltam 50
    cc.refund(conn, cc.get(conn, card.id), p, None, TODAY, NOW)  # estorna o resto
    with pytest.raises(ValueError, match="inteira"):
        cc.refund(conn, cc.get(conn, card.id), p, None, TODAY, NOW)


# --- rotativo -----------------------------------------------------------------


def test_rotativo_feito_a_mao(conn, card):
    buy(conn, card, 8_000)  # dez = 1.920
    cc.pay(conn, cc.get(conn, card.id), 150_000, TODAY, NOW)  # faltam 420
    after_due = date(2026, 12, 11)
    [r] = cc.roll_overdue(conn, cc.get(conn, card.id), after_due, NOW)
    # 420 × 14,5% = 60,90 de juros -> 480,90 na fatura de janeiro
    assert (r.unpaid_cents, r.interest_cents) == (42_000, 6_090)
    assert invoice_totals(conn, card)["2027-01"] == 108_090  # 600 + 480,90
    assert r.invoice.remaining_cents == 0  # dezembro: o resto foi para o rotativo
    assert r.card.used_cents == 959_454 + 8_000 - 150_000 + 6_090
    assert cc.roll_overdue(conn, cc.get(conn, card.id), after_due, NOW) == []  # não repete


# --- desfazer -----------------------------------------------------------------


def test_desfazer_compra_parcelada(conn, card):
    buy(conn, card, 60_000, n=3)
    action = cc.last_user_action(conn)
    r = cc.undo(conn, action)
    assert (r.kind, r.amount_cents, r.invoices) == (
        "compra",
        60_000,
        ["2026-12", "2027-01", "2027-02"],
    )
    assert r.card.used_cents == 959_454
    assert transactions.list_all(conn) == []
    assert invoice_totals(conn, card)["2026-12"] == 184_000


def test_desfazer_pagamento(conn, card):
    cc.pay(conn, card, 300_000, TODAY, NOW)
    r = cc.undo(conn, cc.last_user_action(conn))
    assert (r.kind, r.amount_cents, r.card.used_cents) == ("pagamento", 300_000, 959_454)
    assert all(i.paid_cents == 0 for i in cc.invoices(conn, card.id))


def test_desfazer_bloqueado_por_mudanca_posterior(conn, card):
    buy(conn, card, 8_000)
    action = cc.last_user_action(conn)
    cc.inform_invoice(conn, cc.get(conn, card.id), date(2026, 12, 10), 190_000, NOW)
    with pytest.raises(cc.UndoBlockedError):
        cc.undo(conn, action)


def test_estorno_nao_se_desfaz(conn, card):
    p = buy(conn, card, 8_000).purchase
    cc.refund(conn, cc.get(conn, card.id), p, None, TODAY, NOW)
    with pytest.raises(ValueError, match="estorno"):
        cc.undo(conn, cc.last_user_action(conn))


# --- lembretes, busca, edição e remoção ----------------------------------------


def test_lembrete_da_fatura(conn, card):
    dec7 = date(2026, 12, 7)  # vence 10/12: 3 dias
    [r] = cc.due_reminders(conn, dec7, 3)
    assert (r.card.name, r.invoice.remaining_cents) == ("Nubank", 184_000)
    assert cc.due_reminders(conn, date(2026, 12, 6), 3) == []  # 4 dias: ainda não
    cc.mark_reminded(conn, card.id, date(2026, 12, 10), NOW)
    assert cc.due_reminders(conn, dec7, 3) == []
    assert cc.due_reminders(conn, dec7, 3, include_sent=True)[0].already_sent


def test_fatura_paga_nao_lembra(conn, card):
    cc.pay(conn, card, 184_000, TODAY, NOW)
    assert cc.due_reminders(conn, date(2026, 12, 7), 3) == []


def test_busca_e_mencao(conn, card):
    cc.create(conn, cc.NewCard("Mercado Pago", 28, 5, 300_000), TODAY, NOW)
    assert [c.name for c in cc.find(conn, "nu")] == ["Nubank"]
    assert [c.name for c in cc.mentioned_in(conn, ["gastei", "mercado", "pago"])] == [
        "Mercado Pago"
    ]
    assert cc.mentioned_in(conn, ["gastei", "mercado"]) == []


def test_editar_e_ajustar_utilizado(conn, card):
    c = cc.update_field(conn, card, "limit_cents", 1_500_000, NOW)
    assert c.available_cents == 540_546
    c = cc.update_field(conn, c, "used_cents", 1_000_000, NOW)
    assert c.used_cents == 1_000_000
    cc.create(conn, cc.NewCard("Inter", 3, 10, 100_000), TODAY, NOW)
    with pytest.raises(cc.DuplicateCardError):
        cc.update_field(conn, c, "name", "inter", NOW)  # nome de outro cartão
    assert cc.update_field(conn, c, "name", "NUBANK", NOW).name == "NUBANK"  # o próprio


def test_apagar_cartao_mantem_os_gastos(conn, card):
    buy(conn, card, 8_000)
    cc.delete(conn, cc.get(conn, card.id))
    assert cc.list_cards(conn) == []
    # O gasto continua, mas sem o vínculo com a fatura (ela foi apagada).
    assert transactions.list_all(conn)[0].amount_cents == 8_000
