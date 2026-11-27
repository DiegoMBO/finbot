"""Testes do filtro de segurança (sem conectar ao Telegram).

O guard só lê `update.effective_chat.id`, então um objeto falso simples
(SimpleNamespace) basta. É o mesmo princípio de "mock": testar a regra sem
depender da rede.
"""

import asyncio
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from finbot.bot.security import build_guard

ALLOWED = 111


def run_guard(chat):
    guard = build_guard(ALLOWED).callback
    update = SimpleNamespace(effective_chat=chat)
    # O guard é uma função async; asyncio.run executa e espera o resultado.
    return asyncio.run(guard(update, None))


def test_chat_autorizado_passa():
    assert run_guard(SimpleNamespace(id=ALLOWED)) is None  # não levanta exceção


def test_outro_chat_e_bloqueado():
    with pytest.raises(ApplicationHandlerStop):
        run_guard(SimpleNamespace(id=999))


def test_atualizacao_sem_chat_e_bloqueada():
    with pytest.raises(ApplicationHandlerStop):
        run_guard(None)
