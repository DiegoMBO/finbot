"""Objetos falsos no lugar do Telegram, para testar handlers sem rede.

Os handlers só usam reply_text, callback_query.answer e
edit_message_reply_markup; imitar esses métodos basta.
"""

import asyncio
from types import SimpleNamespace

from finbot import db
from finbot.config import load_settings


class FakeMessage:
    def __init__(self, text=None, log=None):
        self.text = text
        self.log = log if log is not None else []

    async def reply_text(self, text, reply_markup=None):
        self.log.append((text, reply_markup))

    async def reply_photo(self, photo, caption=None):
        self.photo = photo
        self.log.append((caption, None))


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answered = None

    async def answer(self, text=None):
        self.answered = text

    async def edit_message_reply_markup(self, reply_markup=None):
        pass


class Bot:
    """Um "chat" com banco em memória; guarda tudo o que o bot respondeu."""

    def __init__(self):
        self.conn = db.connect(db.MEMORY)
        settings = load_settings({"TELEGRAM_TOKEN": "1:a", "ALLOWED_CHAT_ID": "1"})
        self.sent = []  # mensagens que o bot mandou por conta própria (lembretes)

        async def send_message(chat_id, text):
            self.sent.append((chat_id, text))

        self.context = SimpleNamespace(
            user_data={},
            bot_data={"db": self.conn, "settings": settings},
            args=[],
            bot=SimpleNamespace(send_message=send_message),
        )
        self.log = []

    @property
    def last(self):
        return self.log[-1][0]

    @property
    def last_markup(self):
        return self.log[-1][1]

    def send(self, handler, text="", args=()):
        """Manda uma mensagem (ou comando, com args) para o handler."""
        self.context.args = list(args)
        msg = FakeMessage(text, self.log)
        return asyncio.run(handler(SimpleNamespace(effective_message=msg), self.context))

    def tap(self, handler, data):
        query = FakeQuery(data, FakeMessage(None, self.log))
        update = SimpleNamespace(callback_query=query, effective_message=query.message)
        asyncio.run(handler(update, self.context))
        return query

    def close(self):
        self.conn.close()
