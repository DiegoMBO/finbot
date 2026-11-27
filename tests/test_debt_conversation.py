"""Cadastro guiado de ponta a ponta, com objetos falsos no lugar do Telegram.

Os handlers só usam reply_text, callback_query.answer e edit_message_reply_markup;
imitar esses métodos basta para "conversar" com o bot sem rede.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telegram.ext import ConversationHandler

from finbot import db
from finbot.bot import debts as h
from finbot.config import load_settings
from finbot.services import debts


class FakeMessage:
    def __init__(self, text=None, log=None):
        self.text = text
        self.log = log if log is not None else []

    async def reply_text(self, text, reply_markup=None):
        self.log.append((text, reply_markup))


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answered = None

    async def answer(self, text=None):
        self.answered = text

    async def edit_message_reply_markup(self, reply_markup=None):
        pass


class Chat:
    """Simula a conversa: guarda as respostas do bot e o estado atual."""

    def __init__(self):
        self.conn = db.connect(db.MEMORY)
        settings = load_settings({"TELEGRAM_TOKEN": "1:a", "ALLOWED_CHAT_ID": "1"})
        self.context = SimpleNamespace(
            user_data={}, bot_data={"db": self.conn, "settings": settings}, args=[]
        )
        self.log = []
        self.state = None

    @property
    def last(self):
        return self.log[-1][0]

    def command(self, handler):
        msg = FakeMessage("/cmd", self.log)
        self.state = asyncio.run(handler(SimpleNamespace(effective_message=msg), self.context))

    def say(self, text):
        msg = FakeMessage(text, self.log)
        handler = h.on_answer if self.state == h.ASKING else h.confirm_hint
        self.state = asyncio.run(handler(SimpleNamespace(effective_message=msg), self.context))

    def tap(self, data):
        query = FakeQuery(data, FakeMessage(None, self.log))
        update = SimpleNamespace(callback_query=query, effective_message=query.message)
        handler = h.on_confirm if self.state == h.CONFIRM else h.on_button
        self.state = asyncio.run(handler(update, self.context))
        return query


@pytest.fixture
def chat():
    c = Chat()
    yield c
    c.conn.close()


def test_cadastro_completo_pulando_opcionais(chat):
    chat.command(h.new_debt)
    assert "nome da dívida" in chat.last
    chat.say("Empréstimo Nubank")
    assert "tipo" in chat.last
    chat.tap("divida:kind:emprestimo")
    assert "credor" in chat.last
    chat.say("pular")
    assert "saldo devedor" in chat.last
    chat.say("abc")  # valor inválido: explica e pergunta de novo
    assert "saldo devedor" in chat.last
    assert any("Não entendi" in text for text, _ in chat.log[-3:])
    chat.say("10.000")
    chat.say("2")  # taxa 2% a.m.
    chat.say("12")  # parcelas restantes
    assert "R$ 945,60" in chat.last  # oferece a parcela Price
    chat.tap("divida:skip")  # pula a parcela -> usa a Price
    chat.say("10")  # vencimento
    chat.tap("divida:skip")  # data de início
    assert chat.state == h.CONFIRM
    assert "Parcela: R$ 945,60 (calculada pela tabela Price)" in chat.last
    assert "Credor: -" in chat.last

    chat.tap("divida:save")
    assert chat.state == ConversationHandler.END
    assert "cadastrada" in chat.last
    [d] = debts.list_active(chat.conn)
    assert (d.name, d.balance_cents, d.rate_ppm, d.payment_cents, d.due_day, d.creditor) == (
        "Empréstimo Nubank",
        1_000_000,
        20_000,
        94_560,
        10,
        None,
    )


def test_cartao_pula_parcelas_restantes(chat):
    chat.command(h.new_debt)
    chat.say("Cartão Inter")
    chat.tap("divida:kind:cartao")
    chat.say("Inter")
    chat.say("1.500")
    chat.say("15")  # taxa
    assert "parcela ou do pagamento mensal" in chat.last  # não perguntou "quantas parcelas"


def test_nome_repetido_pede_outro(chat):
    debts.create(chat.conn, debts.NewDebt("Carro", "financiamento", 100), datetime.now(UTC))
    chat.command(h.new_debt)
    chat.say("carro")
    assert "nome da dívida" in chat.last
    assert any("Já existe" in text for text, _ in chat.log[-2:])


def test_campo_obrigatorio_nao_pode_ser_pulado(chat):
    chat.command(h.new_debt)
    query = chat.tap("divida:skip")  # botão de Pular velho, na pergunta do nome
    assert query.answered == "Esse botão é de uma pergunta anterior."
    assert chat.state == h.ASKING


def test_cancelar_nao_salva(chat):
    chat.command(h.new_debt)
    chat.say("Teste")
    chat.command(h.cancel)
    assert chat.state == ConversationHandler.END
    assert "cancelado" in chat.last
    assert debts.list_active(chat.conn) == []


def test_cancelar_na_confirmacao(chat):
    chat.command(h.new_debt)
    chat.say("Tia")
    chat.tap("divida:kind:pessoa")
    for _ in range(7):
        if chat.state != h.ASKING:
            break
        if "saldo" in chat.last:
            chat.say("500")
        else:
            chat.tap("divida:skip")
    assert chat.state == h.CONFIRM
    chat.say("oi")  # texto na confirmação: lembra dos botões
    assert "Salvar" in chat.last
    chat.tap("divida:cancel")
    assert debts.list_active(chat.conn) == []
