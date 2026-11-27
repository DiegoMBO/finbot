"""/fatura e os avisos de rotativo do cartão de crédito."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from telegram import Update
from telegram.ext import ContextTypes

from finbot.config import Settings
from finbot.money import format_brl, format_percent
from finbot.parsing.card import CardArgsError, parse_statement_args
from finbot.parsing.period import month_label
from finbot.services import cards, debts, finance
from finbot.services.cards import Statement
from finbot.services.debts import Debt

USAGE = (
    "Use:\n"
    "/fatura nome total [mínimo] [vencimento]\n"
    "Ex.: /fatura nubank 1.850,40 277,56 10/12\n"
    "/fatura nome: mostra a fatura atual"
)


def _db(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    return context.bot_data["db"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


# --- textos (puros) ---------------------------------------------------------


def statement_label(s: Statement) -> str:
    year, month = (int(p) for p in s.reference_month.split("-"))
    return f"fatura de {month_label(year, month)}"


def due_text(due: date, today: date) -> str:
    days = (due - today).days
    if days > 1:
        return f"vence {due:%d/%m/%Y} (em {days} dias)"
    if days == 1:
        return f"vence {due:%d/%m/%Y} (amanhã)"
    if days == 0:
        return f"vence {due:%d/%m/%Y} (HOJE)"
    return f"venceu em {due:%d/%m/%Y} (há {-days} dia(s))"


def revolving_text(amount_cents: int, rate_ppm: int) -> str:
    """Quanto custa deixar `amount` no rotativo, com os números da conta."""
    if rate_ppm == 0:
        return (
            "Taxa do rotativo não cadastrada: não consigo calcular os juros. "
            "Ela aparece na própria fatura (CET do rotativo)."
        )
    cost = cards.revolving_cost(amount_cents, rate_ppm)
    return (
        f"Taxa do rotativo: {format_percent(rate_ppm)} ao mês "
        f"({format_percent(cost.annual_ppm)} ao ano).\n"
        f"Em 1 mês, {format_brl(amount_cents)} viram "
        f"{format_brl(amount_cents + cost.interest_cents)} "
        f"(+{format_brl(cost.interest_cents)} de juros)."
    )


def format_statement(debt: Debt, s: Statement, today: date) -> str:
    """Situação da fatura atual (para /fatura nome e para o registro)."""
    lines = [f"🧾 {statement_label(s).capitalize()} · {debt.name}"]
    total = f"Total: {format_brl(s.total_cents)}"
    if s.minimum_cents is not None:
        total += f" · mínimo: {format_brl(s.minimum_cents)}"
    lines.append(f"{total} · {due_text(s.due_date, today)}")
    if s.paid_cents:
        lines.append(f"Pago: {format_brl(s.paid_cents)} · falta: {format_brl(s.remaining_cents)}")
    if s.fully_paid:
        lines.append("✅ Paga por completo: nada vai para o rotativo.")
        return "\n".join(lines)
    if s.minimum_cents is not None and s.paid_cents < s.minimum_cents:
        left = s.total_cents - s.minimum_cents
        lines.append(
            f"Se pagar só o mínimo, {format_brl(left)} vão para o rotativo.\n"
            + revolving_text(left, debt.rate_ppm)
        )
    elif s.paid_cents:
        lines.append(revolving_warning(debt, s, today))
    return "\n".join(lines)


def revolving_warning(debt: Debt, s: Statement, today: date) -> str:
    """O aviso EM DESTAQUE de quando o pagamento não cobriu a fatura inteira."""
    if s.fully_paid:
        return f"✅ {statement_label(s).capitalize()} paga por completo: nada vai para o rotativo."
    remaining = s.remaining_cents
    lines = [
        "🚨🚨 ATENÇÃO: ROTATIVO 🚨🚨",
        f"Faltam {format_brl(remaining)} da {statement_label(s)} "
        f"(total {format_brl(s.total_cents)}, pago {format_brl(s.paid_cents)}).",
    ]
    if s.due_date >= today:
        lines.append(
            f"Se não pagar até {s.due_date:%d/%m}, esse valor entra no rotativo, "
            "que tem os juros mais altos do mercado."
        )
    else:
        lines.append(f"A fatura venceu em {s.due_date:%d/%m}: esse valor já está no rotativo.")
    lines.append(revolving_text(remaining, debt.rate_ppm))
    if s.below_minimum:
        lines.append(
            f"⚠️ O pago está abaixo do mínimo ({format_brl(s.minimum_cents)}): "
            "isso conta como atraso, com multa e juros de mora além do rotativo."
        )
    return "\n".join(lines)


def card_status_line(debt: Debt, s: Statement | None, today: date) -> str | None:
    """Uma linha para /dividas e /divida."""
    if s is None:
        return None
    if s.fully_paid:
        return f"🧾 {statement_label(s).capitalize()}: paga ✅"
    return (
        f"🧾 {statement_label(s).capitalize()}: falta {format_brl(s.remaining_cents)}, "
        + due_text(s.due_date, today)
    )


# --- handler ----------------------------------------------------------------


async def statement_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fatura nome [total] [mínimo] [vencimento]."""
    message = update.effective_message
    conn = _db(context)
    today = datetime.now(_settings(context).timezone).date()
    try:
        args = parse_statement_args(list(context.args or []), today)
    except CardArgsError as exc:
        await message.reply_text(f"Não entendi: {exc}.\n\n{USAGE}")
        return

    found = [d for d in debts.find(conn, args.name) if d.kind == "cartao"]
    if len(found) != 1:
        cards_names = ", ".join(d.name for d in debts.list_active(conn) if d.kind == "cartao")
        problem = "Não encontrei o cartão" if not found else "Mais de um cartão combina com"
        await message.reply_text(
            f"{problem} {args.name!r}. Seus cartões: {cards_names or 'nenhum'}.\n"
            "(Cartões são as dívidas cadastradas com o tipo cartão de crédito.)"
        )
        return
    debt = found[0]

    if args.total_cents is None:  # só mostrar
        s = cards.current(conn, debt.id)
        if s is None:
            await message.reply_text(f"Nenhuma fatura registrada para {debt.name}.\n\n{USAGE}")
        else:
            await message.reply_text(format_statement(debt, s, today))
        return

    due = args.due_date
    if due is None:
        if debt.due_day is None:
            await message.reply_text(
                f"{debt.name} não tem dia de vencimento cadastrado: informe a data.\n"
                f"Ex.: /fatura {debt.name} {format_brl(args.total_cents)[3:]} 10/12"
            )
            return
        due = finance.next_due_date(today, debt.due_day)
    try:
        s = cards.register(
            conn, debt.id, args.total_cents, args.minimum_cents, due, datetime.now(UTC)
        )
    except ValueError as exc:
        await message.reply_text(f"Não registrei: {exc}.")
        return
    await message.reply_text("Registrada.\n" + format_statement(debts.get(conn, debt.id), s, today))
