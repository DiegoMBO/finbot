"""Lembretes automáticos de vencimento e o comando /lembretes.

Uma tarefa diária (JobQueue da biblioteca) roda no horário REMINDER_HOUR do
seu fuso. Outra roda uma vez, logo depois que o bot liga, para mandar avisos
que ficaram para trás enquanto ele estava desligado.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time

from telegram import Update
from telegram.ext import Application, ContextTypes

from finbot.bot import card_ops
from finbot.bot import salary as salary_bot
from finbot.config import Settings
from finbot.money import format_brl
from finbot.services import credit_cards as cc
from finbot.services import reminders
from finbot.services.reminders import Reminder

logger = logging.getLogger(__name__)

WEEKDAYS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
STARTUP_DELAY_SECONDS = 15


def when_text(due: date, today: date) -> str:
    days = (due - today).days
    day = f"{due:%d/%m}, {WEEKDAYS[due.weekday()]}"
    if days == 0:
        return f"vence HOJE ({day})"
    if days == 1:
        return f"vence amanhã ({day})"
    return f"vence em {days} dias ({day})"


def format_reminder(r: Reminder, today: date) -> str:
    d = r.debt
    lines = [f"⏰ Lembrete: {d.name} {when_text(r.due_date, today)}."]
    if r.amount_cents:
        lines.append(f"Parcela: {format_brl(r.amount_cents)}.")
        example = f"paguei {format_brl(r.amount_cents)[3:]} do {d.name}"
    else:
        example = f"paguei VALOR do {d.name}"
    lines.append(f"Já pagou? Registre com: {example}")
    return "\n".join(lines)


async def send_pending(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Manda os lembretes pendentes; marca como enviado SÓ depois de enviar."""
    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    today = datetime.now(settings.timezone).date()
    items = reminders.pending(conn, today, settings.timezone, settings.reminder_days_before)
    for r in items:
        await context.bot.send_message(settings.allowed_chat_id, format_reminder(r, today))
        reminders.mark_sent(conn, r.debt.id, r.due_date, datetime.now(UTC))
    # Cartões: rotativo das faturas vencidas e lembretes de fatura.
    sent = len(items) + await card_ops.run_daily(context)
    # Salário: lança o que caiu (salário, vale, 13º, férias) e avisa do prazo das férias.
    sent += await salary_bot.run_daily(context)
    if sent:
        logger.info("Avisos enviados: %d", sent)
    return sent


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_pending(context)


def schedule(app: Application, settings: Settings) -> None:
    at = time(hour=settings.reminder_hour, tzinfo=settings.timezone)
    app.job_queue.run_daily(reminder_job, time=at, name="lembretes-diarios")
    app.job_queue.run_once(reminder_job, when=STARTUP_DELAY_SECONDS, name="lembretes-ao-ligar")


async def reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/lembretes: manda os avisos pendentes agora e lista os próximos vencimentos."""
    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    sent = await send_pending(context)
    today = datetime.now(settings.timezone).date()
    items = reminders.upcoming(conn, today, settings.timezone)
    invoices = cc.due_reminders(conn, today, 31, include_sent=True)
    lines = [
        f"🔔 Lembretes: todo dia às {settings.reminder_hour}h, "
        f"{settings.reminder_days_before} dia(s) antes de cada vencimento."
    ]
    if sent:
        lines.append(f"Acabei de mandar {sent} lembrete(s) pendente(s).")
    if not items and not invoices:
        lines.append(
            "Nenhum vencimento em aberto nos próximos 31 dias "
            "(dívidas sem dia de vencimento não geram lembrete)."
        )
    else:
        lines.append("Próximos vencimentos:")
        for r in items:
            value = f" · {format_brl(r.amount_cents)}" if r.amount_cents else ""
            status = " ✓ avisado" if r.already_sent else ""
            lines.append(f"• {r.debt.name}: {when_text(r.due_date, today)}{value}{status}")
        for r in invoices:
            status = " ✓ avisado" if r.already_sent else ""
            lines.append(
                f"• Fatura {r.card.name}: {when_text(r.invoice.due_date, today)}"
                f" · falta {format_brl(r.invoice.remaining_cents)}{status}"
            )
    await update.effective_message.reply_text("\n".join(lines))
