"""Handlers do módulo de dívidas: cadastro guiado, /dividas e /divida.

Mesma regra de handlers.py: aqui só há conversa com o Telegram e formatação.
Cálculos ficam em services/finance.py; regras e banco em services/debts.py;
o roteiro de perguntas em bot/debt_form.py.
"""

from __future__ import annotations

import sqlite3
import warnings
from datetime import UTC, date, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from finbot.bot import debt_form as form
from finbot.bot.cards import card_status_line
from finbot.config import Settings
from finbot.money import format_brl, format_percent
from finbot.parsing.debt import FieldError, is_skip
from finbot.parsing.period import MONTH_NAMES
from finbot.services import cards, debts, finance
from finbot.services.cards import Statement
from finbot.services.debts import KIND_LABELS, Debt, DebtEvent

ASKING, CONFIRM = range(2)
DRAFT = "debt_draft"  # chave do rascunho em context.user_data
EDITING = "debt_edit"  # chave da edição em andamento (bot/debt_edit.py)
CB = "divida:"  # prefixo dos botões deste módulo

EVENT_LABELS = {
    "cadastro": "cadastro",
    "pagamento": "pagamento",
    "ajuste": "ajuste manual",
    "fatura": "fatura",
}


# --- formatação (funções puras, testáveis sem Telegram) ---------------------


def short_month(d: date) -> str:
    """date(2028, 11, 10) -> "nov/2028"."""
    return f"{MONTH_NAMES[d.month - 1][:3]}/{d.year}"


def describe_projection(
    balance_cents: int,
    rate_ppm: int,
    payment_cents: int | None,
    due_day: int | None,
    today: date,
) -> str:
    """Uma frase com a projeção, ou o aviso de que a dívida não diminui."""
    proj = finance.project(balance_cents, rate_ppm, payment_cents)
    interest = finance.monthly_interest(balance_cents, rate_ppm)
    if proj.status == "quitavel":
        end = finance.payoff_date(today, proj.months, due_day)
        return (
            f"Quitação em {short_month(end)} ({proj.months} pagamento(s)), "
            f"com {format_brl(proj.total_interest_cents)} de juros a pagar."
        )
    if proj.status == "crescendo":
        return (
            f"🚨 A parcela de {format_brl(payment_cents or 0)} não cobre os juros do mês "
            f"({format_brl(interest)}): a dívida CRESCE "
            f"{format_brl(proj.growth_per_month_cents)} por mês."
        )
    if proj.status == "estagnada":
        return (
            f"⚠️ A parcela de {format_brl(payment_cents or 0)} só cobre os juros do mês: "
            "o saldo não diminui."
        )
    if proj.status == "sem_pagamento":
        return "Sem pagamento mensal definido: não dá para estimar a quitação."
    if proj.status == "longa":
        return "Nesse ritmo, leva mais de 100 anos para quitar."
    return "Quitada. 🎉"


def format_rate(rate_ppm: int) -> str:
    if rate_ppm == 0:
        return "sem juros"
    annual = format_percent(finance.annual_equivalent_ppm(rate_ppm))
    return f"{format_percent(rate_ppm)} a.m. ({annual} a.a.)"


def projection_text(d: Debt, today: date) -> str:
    """Projeção da dívida; cartão/cheque especial zerados estão "em dia", não "quitados"."""
    if d.kind in debts.REVOLVING and d.balance_cents == 0:
        return "Em dia: nada em aberto. ✅"
    return describe_projection(d.balance_cents, d.rate_ppm, d.payment_cents, d.due_day, today)


def format_debt_list(
    items: list[Debt], today: date, statements: dict[int, Statement] | None = None
) -> str:
    if not items:
        return "Nenhuma dívida cadastrada. Use /novadivida para cadastrar a primeira."
    lines = [f"💳 Suas dívidas ({len(items)})", ""]
    total_interest = 0
    not_projectable = 0
    for i, d in enumerate(items, 1):
        parts = [f"Saldo {format_brl(d.balance_cents)}"]
        if d.payment_cents:
            parts.append(f"parcela {format_brl(d.payment_cents)}")
        parts.append(f"{format_percent(d.rate_ppm)} a.m." if d.rate_ppm else "sem juros")
        lines.append(f"{i}. {d.name} · {d.kind_label}")
        lines.append("   " + " · ".join(parts))
        projection = debts.projection(d)
        card_line = card_status_line(d, (statements or {}).get(d.id), today)
        if card_line:
            lines.append(f"   {card_line}")
        lines.append(f"   {projection_text(d, today)}")
        if projection.status == "quitavel":
            total_interest += projection.total_interest_cents
        else:
            not_projectable += 1
        lines.append("")
    lines.append(f"Total devido: {format_brl(sum(d.balance_cents for d in items))}")
    lines.append(f"Parcelas por mês: {format_brl(sum(d.payment_cents or 0 for d in items))}")
    interest_line = f"Juros a pagar (estimativa): {format_brl(total_interest)}"
    if not_projectable:
        interest_line += f" (sem contar {not_projectable} dívida(s) sem projeção)"
    lines.append(interest_line)
    lines.append("\nDetalhes: /divida nome")
    return "\n".join(lines)


def format_event(e: DebtEvent, tz) -> str:
    when = e.occurred_at.astimezone(tz).strftime("%d/%m/%Y")
    label = EVENT_LABELS.get(e.kind, e.kind)
    text = f"{when} {label}: saldo {format_brl(e.balance_after_cents)}"
    if e.kind == "pagamento":
        text += f" (pagou {format_brl(e.amount_cents)}"
        text += f", juros {format_brl(e.interest_cents)})" if e.interest_cents else ")"
    return text


def format_debt_details(
    d: Debt,
    history: list[DebtEvent],
    today: date,
    tz,
    statement: Statement | None = None,
) -> str:
    lines = [f"💳 {d.name} ({d.kind_label})"]
    if d.creditor:
        lines.append(f"Credor: {d.creditor}")
    lines.append(f"Saldo: {format_brl(d.balance_cents)}")
    rate_line = f"Juros: {format_rate(d.rate_ppm)}"
    if d.rate_ppm:
        interest = finance.monthly_interest(d.balance_cents, d.rate_ppm)
        rate_line += f", {format_brl(interest)} no mês sobre o saldo atual"
    lines.append(rate_line)
    if d.payment_cents:
        lines.append(f"Parcela: {format_brl(d.payment_cents)}")
    if d.due_day:
        lines.append(f"Vencimento: dia {d.due_day}")
    if d.remaining_installments is not None:
        lines.append(f"Parcelas restantes (contrato): {d.remaining_installments}")
    if d.start_date:
        lines.append(f"Início: {d.start_date:%d/%m/%Y}")

    lines += [
        "",
        "📈 " + projection_text(d, today),
    ]
    card_line = card_status_line(d, statement, today)
    if card_line:
        lines.append(f"{card_line} · detalhes: /fatura {d.name}")
    computed = debts.contract_mismatch(d)
    if computed is not None:
        lines.append(
            f"⚠️ Pelo saldo, taxa e parcela informados seriam {computed} parcelas, não "
            f"{d.remaining_installments}. Confira o saldo e a taxa no app do banco."
        )

    if history:
        lines += ["", "Histórico:"]
        lines += [format_event(e, tz) for e in history[-10:]]
        if len(history) > 10:
            lines.append(f"(mostrando os 10 mais recentes de {len(history)})")
    return "\n".join(lines)


def format_draft(draft: form.Draft, today: date) -> str:
    """Resumo do cadastro para confirmar antes de salvar."""
    lines = ["Confira os dados:", "", f"Nome: {draft['name']}"]
    lines.append(f"Tipo: {KIND_LABELS[draft['kind']]}")
    lines.append(f"Credor: {draft.get('creditor') or '-'}")
    lines.append(f"Saldo: {format_brl(draft['balance_cents'])}")
    lines.append(f"Juros: {format_rate(draft.get('rate_ppm') or 0)}")
    if form.applies(form.step("remaining_installments"), draft):
        lines.append(f"Parcelas restantes: {draft.get('remaining_installments') or '-'}")
    payment = draft.get("payment_cents")
    payment_text = format_brl(payment) if payment else "-"
    if draft.get("payment_computed"):
        payment_text += " (calculada pela tabela Price)"
    lines.append(f"Parcela: {payment_text}")
    lines.append(f"Vencimento: {'dia ' + str(draft['due_day']) if draft.get('due_day') else '-'}")
    start = draft.get("start_date")
    lines.append(f"Início: {start:%d/%m/%Y}" if start else "Início: -")
    projection = describe_projection(
        draft["balance_cents"],
        draft.get("rate_ppm") or 0,
        payment,
        draft.get("due_day"),
        today,
    )
    lines += ["", "📈 " + projection]
    return "\n".join(lines)


# --- teclados ---------------------------------------------------------------


def kind_keyboard(prefix: str = CB) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(label.capitalize(), callback_data=f"{prefix}kind:{key}")
        for key, label in KIND_LABELS.items()
    ]
    return InlineKeyboardMarkup([buttons[i : i + 2] for i in range(0, len(buttons), 2)])


SKIP_KEYBOARD = InlineKeyboardMarkup([[InlineKeyboardButton("Pular ⏭", callback_data=f"{CB}skip")]])
CONFIRM_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Salvar", callback_data=f"{CB}save"),
            InlineKeyboardButton("❌ Cancelar", callback_data=f"{CB}cancel"),
        ]
    ]
)


# --- acesso ao contexto -----------------------------------------------------


def _db(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    return context.bot_data["db"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


def _today(context: ContextTypes.DEFAULT_TYPE) -> date:
    return datetime.now(_settings(context).timezone).date()


# --- cadastro guiado (/novadivida) ------------------------------------------


async def _ask(message: Message, draft: form.Draft) -> int:
    s = form.step(draft["_field"])
    text = s.question(draft)
    if s.field == "kind":
        markup = kind_keyboard()
    elif s.optional:
        text += "\n(opcional: toque em Pular ou escreva pular)"
        markup = SKIP_KEYBOARD
    else:
        markup = None
    await message.reply_text(text, reply_markup=markup)
    return ASKING


async def _advance(
    message: Message, context: ContextTypes.DEFAULT_TYPE, draft: form.Draft, value
) -> int:
    form.apply(draft, form.step(draft["_field"]), value)
    nxt = form.next_step(draft, draft["_field"])
    if nxt is not None:
        draft["_field"] = nxt.field
        return await _ask(message, draft)
    await message.reply_text(format_draft(draft, _today(context)), reply_markup=CONFIRM_KEYBOARD)
    return CONFIRM


async def new_debt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/novadivida: começa (ou recomeça) o cadastro."""
    if context.user_data.get(EDITING):
        await update.effective_message.reply_text(
            "Há uma edição de dívida em andamento. Toque em ✅ Concluir ou mande /cancelar antes."
        )
        return ConversationHandler.END
    first = form.next_step({}, None)
    draft: form.Draft = {"_field": first.field}
    context.user_data[DRAFT] = draft
    await update.effective_message.reply_text(
        "Vamos cadastrar uma dívida, uma pergunta por vez. /cancelar sai a qualquer momento."
    )
    return await _ask(update.effective_message, draft)


async def on_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Resposta digitada para a pergunta atual."""
    message = update.effective_message
    draft = context.user_data.get(DRAFT)
    if draft is None:
        return ConversationHandler.END
    s = form.step(draft["_field"])
    text = message.text or ""

    if s.optional and is_skip(text):
        return await _advance(message, context, draft, None)
    try:
        value = s.parse(text, _today(context))
    except FieldError as exc:
        await message.reply_text(f"Não entendi: {exc}.")
        return await _ask(message, draft)
    if s.field == "name" and debts.name_taken(_db(context), value):
        await message.reply_text(f"Já existe uma dívida chamada {value!r}. Escolha outro nome.")
        return await _ask(message, draft)
    return await _advance(message, context, draft, value)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Botões de tipo e de Pular."""
    query = update.callback_query
    draft = context.user_data.get(DRAFT)
    if draft is None:
        await query.answer("Esse cadastro já terminou.")
        return ConversationHandler.END
    s = form.step(draft["_field"])
    action = query.data.removeprefix(CB)

    if action == "skip" and s.optional:
        value = None
    elif action.startswith("kind:") and s.field == "kind":
        value = action.removeprefix("kind:")
    else:
        await query.answer("Esse botão é de uma pergunta anterior.")
        return ASKING

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)  # tira os botões já usados
    if s.field == "kind":
        await query.message.reply_text(f"Tipo: {KIND_LABELS[value]}")
    return await _advance(query.message, context, draft, value)


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    draft = context.user_data.pop(DRAFT, None)
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    if draft is None or query.data == f"{CB}cancel":
        await query.message.reply_text("Cadastro cancelado. Nada foi salvo.")
        return ConversationHandler.END
    try:
        debt = debts.create(_db(context), form.to_new_debt(draft), datetime.now(UTC))
    except debts.DuplicateDebtError:
        await query.message.reply_text(
            f"Já existe uma dívida chamada {draft['name']!r}. Nada foi salvo; "
            "use /novadivida com outro nome."
        )
        return ConversationHandler.END
    await query.message.reply_text(
        f"✅ Dívida {debt.name!r} cadastrada.\nVeja os detalhes em /divida {debt.name}"
    )
    return ConversationHandler.END


async def confirm_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Toque em ✅ Salvar ou ❌ Cancelar (ou /cancelar).")
    return CONFIRM


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    had_draft = context.user_data.pop(DRAFT, None) is not None
    text = "Cadastro cancelado. Nada foi salvo." if had_draft else "Não há nada para cancelar."
    await update.effective_message.reply_text(text)
    return ConversationHandler.END


async def expired_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botão de um cadastro que já terminou: responde para o Telegram parar de 'carregar'."""
    await update.callback_query.answer("Esse botão expirou.")


def build_conversation() -> ConversationHandler:
    text = filters.TEXT & ~filters.COMMAND
    with warnings.catch_warnings():
        # A biblioteca avisa que, com per_message=False, botões não são rastreados
        # por mensagem. É o que queremos: um cadastro por vez, rastreado pelo chat.
        warnings.filterwarnings("ignore", message=".*per_message=False.*")
        return ConversationHandler(
            entry_points=[CommandHandler("novadivida", new_debt)],
            states={
                ASKING: [
                    MessageHandler(text, on_answer),
                    CallbackQueryHandler(on_button, pattern=f"^{CB}(skip|kind:.+)$"),
                ],
                CONFIRM: [
                    CallbackQueryHandler(on_confirm, pattern=f"^{CB}(save|cancel)$"),
                    MessageHandler(text, confirm_hint),
                ],
            },
            fallbacks=[CommandHandler("cancelar", cancel)],
            allow_reentry=True,  # /novadivida no meio do cadastro recomeça do zero
        )


# --- consultas --------------------------------------------------------------


async def list_debts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dividas: resumo de todas as dívidas ativas."""
    conn = _db(context)
    items = debts.list_active(conn)
    statements = {d.id: s for d in items if d.kind == "cartao" if (s := cards.current(conn, d.id))}
    await update.effective_message.reply_text(format_debt_list(items, _today(context), statements))


async def show_debt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/divida nome: detalhes e histórico de uma dívida."""
    message = update.effective_message
    conn = _db(context)
    query = " ".join(context.args or [])
    if not query:
        await message.reply_text("Use /divida nome. Ex.: /divida nubank\nLista: /dividas")
        return
    found = debts.find(conn, query)
    if not found:
        names = ", ".join(d.name for d in debts.list_active(conn)) or "nenhuma"
        await message.reply_text(f"Não encontrei a dívida {query!r}. Suas dívidas: {names}.")
        return
    if len(found) > 1:
        names = ", ".join(d.name for d in found)
        await message.reply_text(
            f"Mais de uma dívida combina com {query!r}: {names}. Seja mais específico."
        )
        return
    debt = found[0]
    text = format_debt_details(
        debt,
        debts.events(conn, debt.id),
        _today(context),
        _settings(context).timezone,
        cards.current(conn, debt.id) if debt.kind == "cartao" else None,
    )
    await message.reply_text(text)
