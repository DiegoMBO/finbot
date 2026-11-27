"""Etapas 4 a 6: compras, pagamento, /parcelado, /estorno, desfazer, rotativo e lembretes."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fakes import Bot, FakeMessage, FakeQuery

from finbot.bot import card_ops as ops
from finbot.bot import handlers
from finbot.parsing.card_text import parse_card_message
from finbot.services import credit_cards as cc
from finbot.services import transactions

SP = ZoneInfo("America/Sao_Paulo")
NUBANK = {"nubank"}

# --- leitura das mensagens -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "intent", "amount", "n", "category", "description"),
    [
        ("gastei 80 no mercado no nubank", "compra", 8_000, 1, "alimentação", "mercado"),
        ("comprei tv 3000 em 10x no nubank", "compra", 300_000, 10, "outros", "tv"),
        ("gastei 3000 em 10 vezes na loja nubank", "compra", 300_000, 10, "compras", "loja"),
        ("45 farmácia nubank à vista", "compra", 4_500, 1, "saúde", "farmácia"),
        ("paguei 1500 da fatura do nubank", "pagamento", 150_000, 1, None, None),
        ("paguei 1500 nubank", "pagamento", 150_000, 1, None, None),
        ("paguei 50 no mercado no nubank", "ambiguo", 5_000, 1, None, None),
    ],
)
def test_leitura_da_mensagem(text, intent, amount, n, category, description):
    m = parse_card_message(text, NUBANK)
    assert (m.intent, m.entry.amount_cents, m.installments) == (intent, amount, n)
    if category:
        assert (m.entry.category, m.entry.description) == (category, description)


@pytest.mark.parametrize("text", ["nubank", "recebi 100 nubank", "gastei 100 em 60x nubank"])
def test_mensagens_que_nao_sao_do_cartao(text):
    assert parse_card_message(text, NUBANK) is None


# --- pelo chat -------------------------------------------------------------------


@pytest.fixture
def bot():
    b = Bot()
    today = datetime.now(SP).date()
    cc.create(
        b.conn,
        cc.NewCard("Nubank", 3, 10, 1_200_000, 959_454, 184_000, (), 145_000),
        today,
        datetime.now(UTC),
    )
    yield b
    b.close()


def card(bot):
    return cc.list_cards(bot.conn)[0]


def tap(bot, handler, data):
    query = FakeQuery(data, FakeMessage(None, bot.log))
    update = SimpleNamespace(callback_query=query, effective_message=query.message)
    return asyncio.run(handler(update, bot.context))


def test_compra_a_vista_pelo_chat(bot):
    bot.send(handlers.register, "gastei 80 no mercado no nubank")
    assert "💳 Compra no Nubank: R$ 80,00 em alimentação (mercado)" in bot.last
    assert "Usado: R$ 9.594,54 → R$ 9.674,54" in bot.last
    assert "passou de 80% do limite" in bot.last  # 79,95% -> 80,62%
    assert card(bot).used_cents == 967_454
    [t] = transactions.list_all(bot.conn)
    assert (t.amount_cents, t.category) == (8_000, "alimentação")


def test_compra_parcelada_pelo_chat(bot):
    bot.send(handlers.register, "comprei tv 3000 em 10x no nubank")
    assert "10x de R$ 300,00" in bot.last
    assert "Faturas:" in bot.last and "… até" in bot.last
    assert len(transactions.list_all(bot.conn)) == 10


def test_pagamento_parcial_alerta_rotativo(bot):
    bot.send(handlers.register, "paguei 1000 da fatura do nubank")
    text = bot.last
    assert "✅ Pagamento de R$ 1.000,00 no Nubank" in text
    assert "falta R$ 840,00" in text
    assert "ATENÇÃO: ROTATIVO" in text
    # 840 × 14,5% = 121,80 -> 961,80
    assert "R$ 840,00 viram R$ 961,80 (+R$ 121,80)" in text
    assert "não entra como gasto" in text
    assert transactions.list_all(bot.conn) == []


def test_pagamento_total_sem_alerta(bot):
    bot.send(handlers.register, "paguei 1840 nubank")
    assert "paga ✅" in bot.last
    assert "ROTATIVO" not in bot.last


def test_mensagem_ambigua_pergunta(bot):
    bot.send(handlers.register, "paguei 50 no mercado no nubank")
    assert "foi uma compra no Nubank ou o pagamento da fatura?" in bot.last
    tap(bot, handlers.card_choice, f"{ops.CB}compra")
    assert "Compra no Nubank: R$ 50,00" in bot.last


def test_mensagem_ambigua_gasto_comum(bot):
    bot.send(handlers.register, "paguei 50 no mercado no nubank")
    tap(bot, handlers.card_choice, f"{ops.CB}gasto")
    assert "Gasto registrado: R$ 50,00 em alimentação (mercado)" in bot.last
    assert card(bot).used_cents == 959_454  # cartão intocado


def test_desfazer_compra_e_pagamento(bot):
    bot.send(handlers.register, "comprei tênis 600 em 3x no nubank")
    bot.send(handlers.register, "paguei 500 da fatura do nubank")
    bot.send(handlers.undo)
    assert "Desfeito: pagamento de R$ 500,00 no Nubank" in bot.last
    bot.send(handlers.undo)
    assert "Desfeito: compra de R$ 600,00 no Nubank (tênis)" in bot.last
    assert card(bot).used_cents == 959_454
    assert transactions.list_all(bot.conn) == []


def test_desfazer_respeita_a_ordem_entre_cartao_e_gasto_comum(bot):
    bot.send(handlers.register, "gastei 80 no mercado no nubank")
    bot.send(handlers.register, "gastei 20 na padaria")  # gasto comum, depois
    bot.send(handlers.undo)
    assert "Apagado: R$ 20,00" in bot.last
    bot.send(handlers.undo)
    assert "Desfeito: compra de R$ 80,00" in bot.last


# --- /parcelado ------------------------------------------------------------------


def test_parcelado_guiado_com_juros(bot):
    state = bot.send(ops.installment_start)
    assert state == ops.ASK  # um cartão só: pula a escolha
    for answer in ["TV", "pular", "10", "330", "3.000", "hoje"]:
        state = bot.send(ops.installment_answer, answer)
    assert state == ops.CONFIRM
    assert "10x de R$ 330,00 = R$ 3.300,00" in bot.last
    assert "Juros embutidos: R$ 300,00 (~1,77" in bot.last
    tap(bot, ops.installment_confirm, f"{ops.PCB}save")
    assert "Compra no Nubank: R$ 3.300,00" in bot.last
    assert card(bot).used_cents == 959_454 + 330_000


# --- /estorno --------------------------------------------------------------------


def test_estorno_total_pelo_chat(bot):
    bot.send(handlers.register, "comprei tênis 600 em 3x no nubank")
    purchase = cc.purchases(bot.conn, card(bot).id)[0]
    state = bot.send(ops.refund_start)
    assert state == ops.R_PURCHASE
    tap(bot, ops.refund_purchase, f"{ops.RCB}compra:{purchase.id}")
    tap(bot, ops.refund_kind, f"{ops.RCB}total")
    assert "estorno TOTAL de R$ 600,00" in bot.last
    tap(bot, ops.refund_confirm, f"{ops.RCB}ok")
    assert "↩️ Estorno de R$ 600,00 no Nubank" in bot.last
    assert card(bot).used_cents == 959_454
    assert transactions.list_all(bot.conn) == []


def test_estorno_parcial_valor_acima_recusado(bot):
    bot.send(handlers.register, "gastei 80 no mercado no nubank")
    purchase = cc.purchases(bot.conn, card(bot).id)[0]
    bot.send(ops.refund_start)
    tap(bot, ops.refund_purchase, f"{ops.RCB}compra:{purchase.id}")
    tap(bot, ops.refund_kind, f"{ops.RCB}parcial")
    state = bot.send(ops.refund_amount, "100")
    assert state == ops.R_AMOUNT and "máximo" in bot.last
    bot.send(ops.refund_amount, "30")
    tap(bot, ops.refund_confirm, f"{ops.RCB}ok")
    assert "Crédito de R$ 30,00" in bot.last
    assert transactions.list_all(bot.conn)[0].amount_cents == 5_000


def test_desfazer_depois_de_estorno_explica(bot):
    bot.send(handlers.register, "gastei 80 no mercado no nubank")
    purchase = cc.purchases(bot.conn, card(bot).id)[0]
    cc.refund(bot.conn, card(bot), purchase, None, datetime.now(SP).date(), datetime.now(UTC))
    bot.send(handlers.undo)
    assert "estorno não pode ser desfeito" in bot.last


# --- integração com /dividas e /saude --------------------------------------------


def test_dividas_e_saude_mostram_os_cartoes(bot):
    from finbot.bot import debts as debt_bot
    from finbot.bot.health import format_cards_line

    bot.send(debt_bot.list_debts)
    assert "💳 Cartões (1): usado R$ 9.594,54 de R$ 12.000,00" in bot.last
    line = format_cards_line(bot.conn, datetime.now(SP).date())
    assert line.startswith("Cartões: usado R$ 9.594,54 de R$ 12.000,00 (80,0%)")
    assert "faturas atuais a pagar R$ 1.840,00" in line


# --- tarefas diárias -------------------------------------------------------------


def test_rotativo_e_lembrete_automaticos(bot):
    conn = bot.conn
    c = card(bot)
    inv = cc.current_invoice(conn, c, datetime.now(SP).date())
    # Simula que a fatura venceu ontem (sem pagamento): muda as datas dela.
    yesterday = datetime.now(SP).date() - timedelta(days=1)
    conn.execute(
        "UPDATE invoices SET due_date = ?, closing_date = ? WHERE id = ?",
        (yesterday.isoformat(), (yesterday - timedelta(days=7)).isoformat(), inv.id),
    )
    conn.commit()
    sent = asyncio.run(ops.run_daily(bot.context))
    assert sent >= 1
    texts = [t for _, t in bot.sent]
    assert any("🚨 Rotativo no Nubank" in t and "R$ 1.840,00" in t for t in texts)
    # 1.840 × 14,5% = 266,80 de juros
    assert any("R$ 266,80 de juros" in t for t in texts)
    assert asyncio.run(ops.run_daily(bot.context)) == 0 or all(
        "Rotativo" not in t for _, t in bot.sent[len(texts) :]
    )
