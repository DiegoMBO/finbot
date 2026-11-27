"""Testes do /editardivida: regras de atualização e o fluxo pelo "chat"."""

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fakes import Bot, FakeMessage, FakeQuery
from telegram.ext import ConversationHandler

from finbot import db
from finbot.bot import debt_edit as e
from finbot.bot.debts import DRAFT
from finbot.services import cards, debts

NOW = datetime(2026, 11, 26, tzinfo=UTC)
LOAN = debts.NewDebt(
    "Empréstimo Nubank",
    "emprestimo",
    1_000_000,
    creditor="Nubank",
    rate_ppm=30_000,
    payment_cents=94_560,
    due_day=10,
    remaining_installments=12,
)


@pytest.fixture
def conn():
    c = db.connect(db.MEMORY)
    yield c
    c.close()


# --- regras do serviço ------------------------------------------------------


def test_muda_taxa_parcela_vencimento_e_inicio(conn):
    d = debts.create(conn, LOAN, NOW)
    d = debts.update_field(conn, d, "rate_ppm", 25_000)
    d = debts.update_field(conn, d, "payment_cents", 100_000)
    d = debts.update_field(conn, d, "due_day", 15)
    d = debts.update_field(conn, d, "start_date", date(2025, 3, 15))
    assert (d.rate_ppm, d.payment_cents, d.due_day, d.start_date) == (
        25_000,
        100_000,
        15,
        date(2025, 3, 15),
    )
    assert d.balance_cents == 1_000_000  # saldo intocado
    assert [ev.kind for ev in debts.events(conn, d.id)] == ["cadastro"]  # histórico intocado


def test_taxa_em_branco_vira_sem_juros(conn):
    d = debts.create(conn, LOAN, NOW)
    assert debts.update_field(conn, d, "rate_ppm", None).rate_ppm == 0


def test_renomear_atualiza_a_busca_e_recusa_nome_repetido(conn):
    d = debts.create(conn, LOAN, NOW)
    debts.create(conn, debts.NewDebt("Carro", "financiamento", 100), NOW)
    d = debts.update_field(conn, d, "name", "Empréstimo Roxo")
    assert [x.name for x in debts.find(conn, "roxo")] == ["Empréstimo Roxo"]
    assert (
        debts.update_field(conn, d, "name", "EMPRÉSTIMO roxo").name == "EMPRÉSTIMO roxo"
    )  # o próprio
    with pytest.raises(debts.DuplicateDebtError):
        debts.update_field(conn, d, "name", "carro")


def test_virar_cartao_apaga_parcelas_restantes(conn):
    d = debts.create(conn, LOAN, NOW)
    d = debts.update_field(conn, d, "kind", "cartao")
    assert (d.kind, d.remaining_installments) == ("cartao", None)


def test_cartao_com_faturas_nao_troca_de_tipo(conn):
    d = debts.create(conn, debts.NewDebt("Cartão", "cartao", 100, due_day=10), NOW)
    cards.register(conn, d.id, 50_000, None, date(2026, 12, 10), NOW)
    with pytest.raises(ValueError, match="faturas"):
        debts.update_field(conn, d, "kind", "emprestimo")


def test_saldo_nao_e_editavel(conn):
    d = debts.create(conn, LOAN, NOW)
    with pytest.raises(ValueError, match="não pode ser editado"):
        debts.update_field(conn, d, "balance_cents", 1)


# --- textos -----------------------------------------------------------------


def test_menu_mostra_valores_atuais(conn):
    d = debts.create(conn, LOAN, NOW)
    buttons = [row[0].text for row in e.menu_keyboard(d).inline_keyboard]
    assert buttons == [
        "Nome: Empréstimo Nubank",
        "Tipo: empréstimo",
        "Credor: Nubank",
        "Taxa: 3,00% a.m. (42,5761% a.a.)",
        "Parcelas restantes: 12",
        "Parcela: R$ 945,60",
        "Vencimento: dia 10",
        "Início: -",
        "✅ Concluir",
    ]
    cartao = debts.create(conn, debts.NewDebt("Cartão", "cartao", 100), NOW)
    assert "remaining_installments" not in e.editable_fields(cartao)


# --- fluxo pelo chat --------------------------------------------------------


class Flow:
    """Conversa simulada: chama o handler certo para o estado atual."""

    def __init__(self, bot):
        self.bot = bot
        self.state = None

    def command(self, handler, args=()):
        self.bot.context.args = list(args)
        msg = FakeMessage("/cmd", self.bot.log)
        self.state = asyncio.run(handler(SimpleNamespace(effective_message=msg), self.bot.context))

    def tap(self, data):
        query = FakeQuery(data, FakeMessage(None, self.bot.log))
        update = SimpleNamespace(callback_query=query, effective_message=query.message)
        if self.state == e.PICK:
            handler = e.on_pick
        elif self.state == e.ASK:
            handler = e.on_answer_button
        elif data == f"{e.CB}done":
            handler = e.on_done
        else:
            handler = e.on_field
        self.state = asyncio.run(handler(update, self.bot.context))
        return query

    def say(self, text):
        msg = FakeMessage(text, self.bot.log)
        handler = e.on_answer if self.state == e.ASK else e.hint
        self.state = asyncio.run(handler(SimpleNamespace(effective_message=msg), self.bot.context))


@pytest.fixture
def flow():
    bot = Bot()
    debts.create(bot.conn, LOAN, NOW)
    yield Flow(bot)
    bot.close()


def texts(flow, n):
    return "\n".join(t for t, _ in flow.bot.log[-n:])


def test_editar_taxa_pelo_chat(flow):
    flow.command(e.start, ["nubank"])
    assert flow.state == e.MENU
    assert "✏️ Editando: Empréstimo Nubank" in flow.bot.last
    flow.tap(f"{e.CB}field:rate_ppm")
    assert "taxa de juros AO MÊS" in flow.bot.last
    assert "Atual: 3,00% a.m." in flow.bot.last
    flow.say("muito")  # inválido: explica e continua na mesma pergunta
    assert flow.state == e.ASK
    assert "Não entendi" in flow.bot.last
    flow.say("2,5")
    assert flow.state == e.MENU
    assert "✅ Taxa: 3,00% a.m. (42,5761% a.a.) → 2,50% a.m. (34,4889% a.a.)" in texts(flow, 2)
    assert "📈 Quitação em" in texts(flow, 2)
    flow.tap(f"{e.CB}done")
    assert flow.state == ConversationHandler.END
    assert "Pronto" in flow.bot.last
    assert debts.find(flow.bot.conn, "nubank")[0].rate_ppm == 25_000


def test_apagar_parcela_recalcula_pela_price(flow):
    # 10.000 a 3% em 12 parcelas: 10.000 × 0,03 / (1 − 1,03⁻¹²) = 300 / 0,298620 = 1.004,62
    flow.command(e.start, ["nubank"])
    flow.tap(f"{e.CB}field:payment_cents")
    assert "tabela Price: R$ 1.004,62" in flow.bot.last
    flow.tap(f"{e.CB}clear")
    assert "✅ Parcela: R$ 945,60 → R$ 1.004,62" in texts(flow, 2)


def test_trocar_tipo_pelos_botoes(flow):
    flow.command(e.start, ["nubank"])
    flow.tap(f"{e.CB}field:kind")
    flow.tap(f"{e.CB}kind:financiamento")
    assert "✅ Tipo: empréstimo → financiamento" in texts(flow, 2)


def test_varias_dividas_pergunta_qual(flow):
    debts.create(
        flow.bot.conn, debts.NewDebt("Cartão Nubank", "cartao", 100, creditor="Nubank"), NOW
    )
    flow.command(e.start, ["nubank"])
    assert flow.state == e.PICK
    names = [row[0].text for row in flow.bot.last_markup.inline_keyboard]
    assert sorted(names) == ["Cartão Nubank", "Empréstimo Nubank"]
    cartao = debts.find(flow.bot.conn, "cartão nubank")[0]
    flow.tap(f"{e.CB}debt:{cartao.id}")
    assert "✏️ Editando: Cartão Nubank" in flow.bot.last


def test_nao_encontrada_e_cancelar(flow):
    flow.command(e.start, ["itau"])
    assert flow.state == ConversationHandler.END
    assert "Não encontrei" in flow.bot.last
    flow.command(e.start, ["nubank"])
    flow.tap(f"{e.CB}field:due_day")
    flow.command(e.cancel)
    assert flow.state == ConversationHandler.END
    assert debts.find(flow.bot.conn, "nubank")[0].due_day == 10  # nada mudou


def test_nao_edita_durante_um_cadastro(flow):
    flow.bot.context.user_data[DRAFT] = {"_field": "name"}
    flow.command(e.start, ["nubank"])
    assert flow.state == ConversationHandler.END
    assert "cadastro de dívida em andamento" in flow.bot.last
