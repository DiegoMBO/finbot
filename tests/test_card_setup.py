"""Etapa 3: /novocartao, /editarcartao, /cartoes, /cartao e /fatura pelo "chat"."""

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fakes import Bot, FakeMessage, FakeQuery
from telegram.ext import ConversationHandler

from finbot.bot import card_form as form
from finbot.bot import card_setup as setup
from finbot.bot import card_views as views
from finbot.parsing.debt import FieldError, parse_money_list
from finbot.services import credit_cards as cc

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2026, 11, 27)
NOW = datetime(2026, 11, 27, 15, tzinfo=UTC)


class Flow:
    def __init__(self, bot):
        self.bot = bot
        self.state = None

    def command(self, handler, args=()):
        self.bot.context.args = list(args)
        msg = FakeMessage("/cmd", self.bot.log)
        self.state = asyncio.run(handler(SimpleNamespace(effective_message=msg), self.bot.context))

    def say(self, text, handler):
        msg = FakeMessage(text, self.bot.log)
        self.state = asyncio.run(handler(SimpleNamespace(effective_message=msg), self.bot.context))

    def tap(self, data, handler):
        query = FakeQuery(data, FakeMessage(None, self.bot.log))
        update = SimpleNamespace(callback_query=query, effective_message=query.message)
        self.state = asyncio.run(handler(update, self.bot.context))


@pytest.fixture
def flow():
    bot = Bot()
    yield Flow(bot)
    bot.close()


# --- roteiro -------------------------------------------------------------------


def test_lista_de_valores():
    assert parse_money_list("600 400,50; 250") == (60_000, 40_050, 25_000)
    with pytest.raises(FieldError):
        parse_money_list("   ")


def test_ordem_das_perguntas_e_sem_faturas_quando_nada_usado():
    def fields(draft):
        out, cur = [], None
        while (s := form.next_step(draft, cur)) is not None:
            out.append(s.field)
            cur = s.field
        return out

    assert fields({"used_cents": 100}) == [
        "name",
        "closing_day",
        "due_day",
        "limit_cents",
        "used_cents",
        "current_invoice_cents",
        "next_invoices_cents",
        "rate_ppm",
    ]
    assert "current_invoice_cents" not in fields({"used_cents": 0})


def test_validacoes_que_dependem_das_respostas():
    assert form.step("due_day").check({"closing_day": 3}, 3) is not None
    assert form.step("current_invoice_cents").check({"used_cents": 1_000}, 2_000) is not None
    draft = {"used_cents": 10_000, "current_invoice_cents": 6_000}
    assert form.step("next_invoices_cents").check(draft, (3_000, 2_000)) is not None
    assert form.step("next_invoices_cents").check(draft, (3_000, 1_000)) is None


def test_pergunta_da_fatura_mostra_o_vencimento():
    draft = {"closing_day": 3, "due_day": 10}
    assert "vence em 10/12/2026" in form.step("current_invoice_cents").question(draft, TODAY)
    assert "jan/2027, fev/2027, mar/2027" in form.step("next_invoices_cents").question(draft, TODAY)


# --- cadastro pelo chat ----------------------------------------------------------


def test_cadastro_completo(flow):
    flow.command(setup.new_card)
    assert "Nome do cartão" in flow.bot.last
    flow.say("Nubank", setup.on_answer)
    flow.say("3", setup.on_answer)  # fechamento
    flow.say("3", setup.on_answer)  # vencimento igual: recusa
    assert "mesmo dia" in "\n".join(t for t, _ in flow.bot.log[-2:])
    flow.say("10", setup.on_answer)
    flow.say("12.000", setup.on_answer)
    flow.say("9.594,54", setup.on_answer)
    flow.say("20.000", setup.on_answer)  # fatura maior que o usado: recusa
    assert "maior que o utilizado" in "\n".join(t for t, _ in flow.bot.log[-2:])
    flow.say("1.840", setup.on_answer)
    flow.say("600 400", setup.on_answer)
    flow.tap(f"{setup.CB}skip", setup.on_skip)  # rotativo
    assert flow.state == setup.CONFIRM
    assert "Sem mês definido" in flow.bot.last
    flow.tap(f"{setup.CB}save", setup.on_confirm)
    assert flow.state == ConversationHandler.END
    assert "Cartão Nubank cadastrado" in flow.bot.last
    [card] = cc.list_cards(flow.bot.conn)
    assert (card.limit_cents, card.used_cents, card.rate_ppm) == (1_200_000, 959_454, 0)
    assert len(cc.invoices(flow.bot.conn, card.id)) == 3


def test_nome_repetido(flow):
    cc.create(flow.bot.conn, cc.NewCard("Nubank", 3, 10, 100_000), TODAY, NOW)
    flow.command(setup.new_card)
    flow.say("nubank", setup.on_answer)
    assert "já existe" in "\n".join(t for t, _ in flow.bot.log[-2:])


def test_nao_cadastra_durante_outro_fluxo(flow):
    flow.bot.context.user_data["debt_draft"] = {"_field": "name"}
    flow.command(setup.new_card)
    assert flow.state == ConversationHandler.END
    assert "em andamento" in flow.bot.last


# --- edição ----------------------------------------------------------------------


def test_editar_limite_e_utilizado(flow):
    cc.create(flow.bot.conn, cc.NewCard("Nubank", 3, 10, 1_200_000, 959_454), TODAY, NOW)
    flow.command(setup.edit_card, ["nubank"])
    assert flow.state == setup.MENU
    flow.tap(f"{setup.EDIT_CB}field:limit_cents", setup.on_field)
    flow.say("15.000", setup.on_edit_answer)
    assert "Limite: R$ 12.000,00 → R$ 15.000,00" in "\n".join(t for t, _ in flow.bot.log[-2:])
    flow.tap(f"{setup.EDIT_CB}field:due_day", setup.on_field)
    flow.say("3", setup.on_edit_answer)  # igual ao fechamento
    assert flow.state == setup.EDIT_ASK
    flow.say("12", setup.on_edit_answer)
    flow.tap(f"{setup.EDIT_CB}done", setup.on_done)
    card = cc.list_cards(flow.bot.conn)[0]
    assert (card.limit_cents, card.due_day) == (1_500_000, 12)


# --- consultas -------------------------------------------------------------------


@pytest.fixture
def two_cards(flow):
    conn = flow.bot.conn
    today = datetime.now(SP).date()
    nu = cc.create(
        conn,
        cc.NewCard("Nubank", 3, 10, 1_200_000, 959_454, 184_000, (60_000, 40_000), 145_000),
        today,
        NOW,
    )
    mp = cc.create(conn, cc.NewCard("Mercado Pago", 28, 5, 300_000, 160_000), today, NOW)
    return nu, mp


def test_cartoes_mostra_resumo_e_melhor_cartao(flow, two_cards):
    flow.command(views.cards_command)
    text = flow.bot.last
    assert "💳 Nubank · fecha dia 3 · vence dia 10" in text
    assert "disponível R$ 2.405,46" in text
    assert "💳 Mercado Pago" in text
    assert "⭐ Melhor cartão para comprar hoje:" in text


def test_cartao_detalhes(flow, two_cards):
    flow.command(views.card_command, ["nubank"])
    text = flow.bot.last
    assert "rotativo 14,50% a.m." in text
    assert "Sem mês definido: R$ 6.754,54" in text
    assert "📦 R$ 1.840,00 saldo de antes do bot" in text
    flow.command(views.card_command, ["itau"])
    assert "/novocartao" in flow.bot.last


def test_fatura_mostra_e_informa_valor_do_banco(flow, two_cards):
    flow.command(views.invoice_command, ["nubank"])
    assert "R$ 1.840,00" in flow.bot.last
    flow.command(views.invoice_command, ["nubank", "1.870"])
    assert "Valor do banco registrado" in flow.bot.last
    assert "valor do banco" in flow.bot.last
    # Mercado Pago: nada lançado na fatura atual
    flow.command(views.invoice_command, ["mercado", "pago"])
    assert "Nada lançado" in flow.bot.last


def test_status_da_fatura():
    inv = cc.Invoice(1, 1, "2026-12", date(2026, 12, 3), date(2026, 12, 10), 192_000, None, 0, None)
    text = views.invoice_status(inv, TODAY)
    assert text == "R$ 1.920,00 · vence 10/12/2026 (em 13 dias) · aberta (fecha 03/12)"
    paid = cc.Invoice(
        1, 1, "2026-12", date(2026, 12, 3), date(2026, 12, 10), 192_000, None, 192_000, None
    )
    assert "paga ✅" in views.invoice_status(paid, date(2026, 12, 5))
    assert "aberta" not in views.invoice_status(paid, date(2026, 12, 5))
