"""Filtro de segurança: só o chat autorizado conversa com o bot.

Decisão: em vez de colocar um filtro em cada handler (e correr o risco de esquecer
em um handler novo), registramos UM handler no grupo -1. Na python-telegram-bot,
os grupos de handlers rodam em ordem crescente, então o grupo -1 vê toda
atualização (mensagens, comandos, cliques em botões) antes de todo o resto.
Se o chat não for o autorizado, lançamos `ApplicationHandlerStop`, que
interrompe o processamento: nenhum outro handler roda e o bot não responde nada.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes, TypeHandler

logger = logging.getLogger(__name__)


def build_guard(allowed_chat_id: int) -> TypeHandler:
    """Cria o handler que bloqueia qualquer chat diferente de `allowed_chat_id`."""

    async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None or chat.id != allowed_chat_id:
            # Registramos no log local (só você vê). Isso também ajuda caso o
            # ALLOWED_CHAT_ID esteja errado: o id real aparece no terminal.
            logger.warning(
                "Atualização ignorada de chat não autorizado: chat_id=%s",
                chat.id if chat else None,
            )
            raise ApplicationHandlerStop  # silêncio total para quem não é você

    # TypeHandler(Update, ...) captura QUALQUER tipo de atualização.
    return TypeHandler(Update, guard)
