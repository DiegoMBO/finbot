"""Consultas de cartão: /cartoes, /cartao nome e /fatura nome [valor] [vencimento]."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from telegram import Update
from telegram.ext import ContextTypes

from finbot.config import Settings
from finbot.money import format_brl, format_percent
from finbot.parsing.card import CardArgsError, parse_statement_args
from finbot.parsing.period import MONTH_NAMES, month_label
from finbot.services import card_math as cm
from finbot.services import credit_cards as cc
from finbot.services.credit_cards import Card, Invoice

ITEM_ICONS = {"compra": "🛒", "anterior": "📦", "estorno": "↩️", "rotativo": "🚨"}


def _db(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    return context.bot_data["db"]


def _today(context: ContextTypes.DEFAULT_TYPE) -> date:
    settings: Settings = context.bot_data["settings"]
    return datetime.now(settings.timezone).date()


# --- textos (puros) ---------------------------------------------------------


def due_text(due: date, today: date) -> str:
    days = (due - today).days
    if days > 1:
        return f"vence {due:%d/%m/%Y} (em {days} dias)"
    if days == 1:
        return f"vence {due:%d/%m/%Y} (amanhã)"
    if days == 0:
        return f"vence {due:%d/%m/%Y} (HOJE)"
    return f"venceu em {due:%d/%m/%Y} (há {-days} dia(s))"


def month_short(reference_month: str) -> str:
    year, month = (int(x) for x in reference_month.split("-"))
    return f"{MONTH_NAMES[month - 1][:3]}/{year}"


def invoice_label(inv: Invoice) -> str:
    year, month = (int(x) for x in inv.reference_month.split("-"))
    return f"fatura de {month_label(year, month)}"


def usage_text(card: Card) -> str:
    bp = card.usage_bp
    pct = f"{bp // 100},{(bp % 100) // 10}%"
    icon = " 🚨" if bp >= 9_000 else " ⚠️" if bp >= 8_000 else ""
    return (
        f"Limite {format_brl(card.limit_cents)} · usado {format_brl(card.used_cents)} ({pct}){icon}"
        f" · disponível {format_brl(card.available_cents)}"
    )


def invoice_status(inv: Invoice, today: date) -> str:
    """Ex.: "R$ 1.920,00 · vence 10/12/2026 (em 13 dias) · aberta (fecha 03/12)"."""
    parts = [format_brl(inv.total_cents)]
    if inv.paid_cents:
        parts.append(f"pago {format_brl(inv.paid_cents)}, falta {format_brl(inv.remaining_cents)}")
    if inv.rolled_at:
        parts.append(f"o que faltou ({format_brl(inv.unpaid_cents)}) foi para o rotativo")
    elif inv.total_cents and inv.remaining_cents == 0:
        parts.append("paga ✅")
    parts.append(due_text(inv.due_date, today))
    if not inv.is_closed(today):
        parts.append(f"aberta (fecha {inv.closing_date:%d/%m})")
    if inv.informed:
        parts.append("valor do banco")
    return " · ".join(parts)


def format_card_summary(conn: sqlite3.Connection, card: Card, today: date) -> list[str]:
    lines = [f"💳 {card.name} · fecha dia {card.closing_day} · vence dia {card.due_day}"]
    lines.append(f"   {usage_text(card)}")
    upcoming = cc.upcoming_invoices(conn, card, today, 2)
    current = cc.current_invoice(conn, card, today)
    if current is None:
        due = cc.payable_cycle(card, today).due
        lines.append(f"   Fatura atual (vence {due:%d/%m}): nada lançado")
    else:
        lines.append(f"   Fatura atual: {invoice_status(current, today)}")
    following = [i for i in upcoming if current is None or i.id != current.id]
    if following:
        nxt = following[0]
        lines.append(
            f"   Próxima ({month_short(nxt.reference_month)}): {format_brl(nxt.total_cents)}"
        )
    for inv in cc.overdue_invoices(conn, card, today):
        label = invoice_label(inv).capitalize()
        lines.append(f"   🚨 {label} VENCIDA: falta {format_brl(inv.remaining_cents)}")
    return lines


def format_cards(conn: sqlite3.Connection, cards: list[Card], today: date) -> str:
    if not cards:
        return "Nenhum cartão cadastrado. Use /novocartao."
    lines = []
    for card in cards:
        lines += format_card_summary(conn, card, today) + [""]
    best = cm.best_card([(c.name, c.closing_day, c.due_day) for c in cards], today)
    if best and len(cards) > 1:
        name, due = best
        lines.append(
            f"⭐ Melhor cartão para comprar hoje: {name} (a compra só vence em {due:%d/%m/%Y})"
        )
    lines.append("Detalhes: /cartao nome")
    return "\n".join(lines)


def format_invoice_items(conn: sqlite3.Connection, inv: Invoice, limit: int = 8) -> list[str]:
    rows = conn.execute(
        "SELECT kind, amount_cents, description FROM invoice_items WHERE invoice_id = ?"
        " ORDER BY id DESC LIMIT ?",
        (inv.id, limit),
    ).fetchall()
    lines = []
    for r in rows:
        icon = ITEM_ICONS.get(r["kind"], "•")
        lines.append(f"   {icon} {format_brl(r['amount_cents'])} {r['description']}".rstrip())
    return lines


def format_card_details(conn: sqlite3.Connection, card: Card, today: date) -> str:
    rate = (
        f"rotativo {format_percent(card.rate_ppm)} a.m."
        if card.rate_ppm
        else "rotativo: taxa não cadastrada"
    )
    lines = [
        f"💳 {card.name}",
        usage_text(card),
        f"Fecha dia {card.closing_day} · vence dia {card.due_day} · {rate}",
        "",
    ]
    for inv in cc.overdue_invoices(conn, card, today):
        lines.append(f"🚨 {invoice_label(inv).capitalize()}: {invoice_status(inv, today)}")
    current = cc.current_invoice(conn, card, today)
    if current is None:
        lines.append(
            f"🧾 Fatura atual (vence {cc.payable_cycle(card, today).due:%d/%m}): nada lançado"
        )
    else:
        lines.append(f"🧾 {invoice_label(current).capitalize()}: {invoice_status(current, today)}")
        lines += format_invoice_items(conn, current, 6)
    following = [
        i
        for i in cc.upcoming_invoices(conn, card, today, 5)
        if current is None or i.id != current.id
    ]
    if following:
        lines.append(
            "Próximas: "
            + " · ".join(
                f"{month_short(i.reference_month)} {format_brl(i.total_cents)}" for i in following
            )
        )
    unallocated = cc.unallocated_cents(conn, card)
    if unallocated:
        lines.append(
            f"Sem mês definido: {format_brl(unallocated)} (parcelas de antes do bot; "
            "informe cada fatura com /fatura quando fechar)"
        )
    plans = cc.active_installment_plans(conn, card, today)
    if plans:
        lines += ["", "Parcelamentos em andamento:"]
        for plan in plans:
            lines.append(
                f"• {plan['description'] or 'compra'}: faltam {plan['left_count']} de "
                f"{plan['installments']} ({format_brl(plan['left_cents'])})"
            )
    return "\n".join(lines)


def format_invoice(conn: sqlite3.Connection, card: Card, inv: Invoice, today: date) -> str:
    lines = [f"🧾 {invoice_label(inv).capitalize()} · {card.name}", invoice_status(inv, today)]
    items = format_invoice_items(conn, inv, 15)
    if items:
        lines += ["Lançamentos (mais recentes primeiro):"] + items
    if inv.informed and inv.items_cents != inv.total_cents:
        lines.append(f"(lançado pelo bot: {format_brl(inv.items_cents)}; vale o valor do banco)")
    return "\n".join(lines)


# --- handlers ---------------------------------------------------------------


def find_one(conn: sqlite3.Connection, name: str) -> tuple[Card | None, str | None]:
    """Devolve (cartão, None) ou (None, mensagem de erro)."""
    found = cc.find(conn, name)
    if len(found) == 1:
        return found[0], None
    names = ", ".join(c.name for c in (found or cc.list_cards(conn))) or "nenhum"
    if not found:
        return (
            None,
            f"Não encontrei o cartão {name!r}. Seus cartões: {names}.\nCadastrar: /novocartao",
        )
    return None, f"Mais de um cartão combina com {name!r}: {names}. Seja mais específico."


async def cards_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = _db(context)
    await update.effective_message.reply_text(
        format_cards(conn, cc.list_cards(conn), _today(context))
    )


async def card_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    name = " ".join(context.args or [])
    if not name:
        await message.reply_text("Use /cartao nome. Ex.: /cartao nubank\nTodos: /cartoes")
        return
    conn = _db(context)
    card, error = find_one(conn, name)
    if error:
        await message.reply_text(error)
        return
    await message.reply_text(format_card_details(conn, card, _today(context)))


INVOICE_USAGE = (
    "Use:\n"
    "/fatura nome: mostra a fatura atual\n"
    "/fatura nome valor: informa o total que o banco cobrou na fatura atual\n"
    "/fatura nome valor 10/01: informa o total de outra fatura (pelo vencimento)"
)


async def invoice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fatura nome [valor] [vencimento]."""
    message = update.effective_message
    conn = _db(context)
    today = _today(context)
    try:
        args = parse_statement_args(list(context.args or []), today)
    except CardArgsError as exc:
        await message.reply_text(f"Não entendi: {exc}.\n\n{INVOICE_USAGE}")
        return
    card, error = find_one(conn, args.name)
    if error:
        await message.reply_text(error)
        return
    due = args.due_date or cc.payable_cycle(card, today).due

    if args.total_cents is None:
        inv = cc.invoice_for_due(conn, card.id, due)
        if inv is None:
            await message.reply_text(
                f"Nada lançado na fatura de {card.name} que vence em {due:%d/%m/%Y}."
                f"\n\n{INVOICE_USAGE}"
            )
            return
        await message.reply_text(format_invoice(conn, card, inv, today))
        return

    inv, adjustment = cc.inform_invoice(conn, card, due, args.total_cents, datetime.now(UTC))
    lines = ["✅ Valor do banco registrado.", format_invoice(conn, card, inv, today)]
    if adjustment:
        sign = "+" if adjustment > 0 else "-"
        lines.append(
            f"Utilizado ajustado em {sign}{format_brl(abs(adjustment))} "
            "(diferença que o bot não conhecia, como anuidade, juros ou IOF)."
        )
    await message.reply_text("\n".join(lines))
