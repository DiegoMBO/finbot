"""/novocartao (cadastro guiado) e /editarcartao (menu de campos)."""

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

from finbot.bot import card_form as form
from finbot.bot.card_views import format_card_details, usage_text
from finbot.config import Settings
from finbot.money import format_brl, format_percent
from finbot.parsing import debt as p
from finbot.parsing.debt import FieldError, is_skip
from finbot.services import credit_cards as cc
from finbot.services.credit_cards import Card

ASKING, CONFIRM = range(2)
MENU, EDIT_ASK, PICK = range(2, 5)
DRAFT = "card_draft"
EDITING = "card_edit"
CB = "novocartao:"
EDIT_CB = "editarcartao:"
BUSY_KEYS = ("debt_draft", "debt_edit", DRAFT, EDITING)


def _db(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    return context.bot_data["db"]


def _today(context: ContextTypes.DEFAULT_TYPE) -> date:
    settings: Settings = context.bot_data["settings"]
    return datetime.now(settings.timezone).date()


def _busy(context, own_key: str) -> bool:
    """Já há outro cadastro/edição em andamento?"""
    return any(context.user_data.get(k) for k in BUSY_KEYS if k != own_key)


SKIP = InlineKeyboardMarkup([[InlineKeyboardButton("Pular ⏭", callback_data=f"{CB}skip")]])
CONFIRM_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Salvar", callback_data=f"{CB}save"),
            InlineKeyboardButton("❌ Cancelar", callback_data=f"{CB}cancel"),
        ]
    ]
)


# --- textos -----------------------------------------------------------------


def format_draft(draft: form.Draft, today: date) -> str:
    lines = [
        "Confira o cartão:",
        "",
        f"Nome: {draft['name']}",
        f"Fecha dia {draft['closing_day']} · vence dia {draft['due_day']}",
        f"Limite: {format_brl(draft['limit_cents'])}",
        f"Usado: {format_brl(draft.get('used_cents') or 0)}",
    ]
    current = draft.get("current_invoice_cents")
    due = form.current_due(draft, today)
    lines.append(f"Fatura atual (vence {due:%d/%m/%Y}): {format_brl(current) if current else '-'}")
    nexts = draft.get("next_invoices_cents") or ()
    if nexts:
        dues = form.next_dues(draft, today, len(nexts))
        lines.append(
            "Próximas: "
            + " · ".join(f"{d:%m/%Y} {format_brl(v)}" for d, v in zip(dues, nexts, strict=True))
        )
    informed = (current or 0) + sum(nexts)
    rest = (draft.get("used_cents") or 0) - informed
    if rest > 0:
        lines.append(f"Sem mês definido: {format_brl(rest)}")
    rate = draft.get("rate_ppm")
    lines.append(f"Rotativo: {format_percent(rate) + ' a.m.' if rate else 'não informado'}")
    return "\n".join(lines)


# --- cadastro ---------------------------------------------------------------


async def _ask(message: Message, draft: form.Draft, today: date) -> int:
    s = form.step(draft["_field"])
    text = s.question(draft, today)
    if s.optional:
        text += "\n(opcional: toque em Pular ou escreva pular)"
    await message.reply_text(text, reply_markup=SKIP if s.optional else None)
    return ASKING


async def _advance(message: Message, context, draft: form.Draft, value) -> int:
    draft[draft["_field"]] = value
    nxt = form.next_step(draft, draft["_field"])
    if nxt is not None:
        draft["_field"] = nxt.field
        return await _ask(message, draft, _today(context))
    await message.reply_text(format_draft(draft, _today(context)), reply_markup=CONFIRM_KEYBOARD)
    return CONFIRM


async def new_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if _busy(context, DRAFT):
        await message.reply_text(
            "Há outro cadastro ou edição em andamento. Termine ou use /cancelar."
        )
        return ConversationHandler.END
    draft: form.Draft = {"_field": form.next_step({}, None).field}
    context.user_data[DRAFT] = draft
    await message.reply_text("Vamos cadastrar um cartão. /cancelar sai a qualquer momento.")
    return await _ask(message, draft, _today(context))


async def on_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    draft = context.user_data.get(DRAFT)
    if draft is None:
        return ConversationHandler.END
    s = form.step(draft["_field"])
    text = message.text or ""
    if s.optional and is_skip(text):
        return await _advance(message, context, draft, None)
    try:
        value = s.parse(text)
    except FieldError as exc:
        await message.reply_text(f"Não entendi: {exc}.")
        return await _ask(message, draft, _today(context))
    problem = s.check(draft, value)
    if problem is None and s.field == "name" and cc.name_taken(_db(context), value):
        problem = f"já existe um cartão chamado {value!r}"
    if problem:
        await message.reply_text(f"Não deu: {problem}.")
        return await _ask(message, draft, _today(context))
    return await _advance(message, context, draft, value)


async def on_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    draft = context.user_data.get(DRAFT)
    if draft is None or not form.step(draft["_field"]).optional:
        await query.answer("Esse botão é de uma pergunta anterior.")
        return ASKING if draft else ConversationHandler.END
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    return await _advance(query.message, context, draft, None)


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    draft = context.user_data.pop(DRAFT, None)
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    if draft is None or query.data == f"{CB}cancel":
        await query.message.reply_text("Cadastro cancelado. Nada foi salvo.")
        return ConversationHandler.END
    try:
        card = cc.create(_db(context), form.to_new_card(draft), _today(context), datetime.now(UTC))
    except ValueError as exc:
        await query.message.reply_text(f"Não salvei: {exc}. Use /novocartao de novo.")
        return ConversationHandler.END
    await query.message.reply_text(
        f"✅ Cartão {card.name} cadastrado.\n"
        f"Para registrar compras, cite o cartão: gastei 80 no mercado no {card.name}\n"
        f"Parcelado: comprei tv 3000 em 10x no {card.name} (ou /parcelado)\n"
        f"Detalhes: /cartao {card.name}"
    )
    return ConversationHandler.END


async def confirm_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Toque em ✅ Salvar ou ❌ Cancelar (ou /cancelar).")
    return CONFIRM


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    had = context.user_data.pop(DRAFT, None) or context.user_data.pop(EDITING, None)
    await update.effective_message.reply_text(
        "Cancelado. Nada foi salvo." if had else "Não há nada para cancelar."
    )
    return ConversationHandler.END


def _quiet_conversation(**kwargs) -> ConversationHandler:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*per_message=False.*")
        return ConversationHandler(**kwargs)


def build_new_card_conversation() -> ConversationHandler:
    text = filters.TEXT & ~filters.COMMAND
    return _quiet_conversation(
        entry_points=[CommandHandler("novocartao", new_card)],
        states={
            ASKING: [
                MessageHandler(text, on_answer),
                CallbackQueryHandler(on_skip, pattern=f"^{CB}skip$"),
            ],
            CONFIRM: [
                CallbackQueryHandler(on_confirm, pattern=f"^{CB}(save|cancel)$"),
                MessageHandler(text, confirm_hint),
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancel)],
        allow_reentry=True,
    )


# --- edição -----------------------------------------------------------------

EDIT_FIELDS = {
    "name": ("Nome", p.parse_name, "Novo nome do cartão?"),
    "closing_day": ("Fechamento", p.parse_day, "Novo dia de fechamento? (1 a 31)"),
    "due_day": ("Vencimento", p.parse_day, "Novo dia de vencimento? (1 a 31)"),
    "limit_cents": ("Limite", p.parse_money, "Novo limite total? Ex.: 15.000"),
    "used_cents": (
        "Utilizado",
        p.parse_money_or_zero,
        "Quanto do limite está usado agora, pelo app do banco? Ex.: 9.320,10",
    ),
    "rate_ppm": ("Rotativo", p.parse_rate, "Taxa do rotativo ao mês? Ex.: 14,5"),
}


def field_value(card: Card, field: str) -> str:
    return {
        "name": card.name,
        "closing_day": f"dia {card.closing_day}",
        "due_day": f"dia {card.due_day}",
        "limit_cents": format_brl(card.limit_cents),
        "used_cents": format_brl(card.used_cents),
        "rate_ppm": f"{format_percent(card.rate_ppm)} a.m." if card.rate_ppm else "-",
    }[field]


def edit_menu(card: Card) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{label}: {field_value(card, f)}", callback_data=f"{EDIT_CB}field:{f}"
            )
        ]
        for f, (label, _, _) in EDIT_FIELDS.items()
    ]
    rows.append([InlineKeyboardButton("✅ Concluir", callback_data=f"{EDIT_CB}done")])
    return InlineKeyboardMarkup(rows)


async def _show_menu(message: Message, context) -> int:
    card = cc.get(_db(context), context.user_data[EDITING]["card_id"])
    await message.reply_text(
        f"✏️ Editando: {card.name}\n{usage_text(card)}\n\n"
        "Mudar fechamento/vencimento vale para as próximas compras.\nToque no que quer mudar:",
        reply_markup=edit_menu(card),
    )
    return MENU


async def edit_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if _busy(context, EDITING):
        await message.reply_text(
            "Há outro cadastro ou edição em andamento. Termine ou use /cancelar."
        )
        return ConversationHandler.END
    conn = _db(context)
    query = " ".join(context.args or [])
    found = cc.find(conn, query) if query else cc.list_cards(conn)
    if not found:
        await message.reply_text("Nenhum cartão encontrado. Seus cartões: /cartoes")
        return ConversationHandler.END
    if len(found) > 1:
        context.user_data[EDITING] = {"card_id": None}
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(c.name, callback_data=f"{EDIT_CB}card:{c.id}")] for c in found]
        )
        await message.reply_text("Qual cartão você quer editar?", reply_markup=keyboard)
        return PICK
    context.user_data[EDITING] = {"card_id": found[0].id}
    return await _show_menu(message, context)


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    context.user_data[EDITING] = {"card_id": int(query.data.removeprefix(f"{EDIT_CB}card:"))}
    return await _show_menu(query.message, context)


async def on_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    field = query.data.removeprefix(f"{EDIT_CB}field:")
    context.user_data[EDITING]["field"] = field
    card = cc.get(_db(context), context.user_data[EDITING]["card_id"])
    _, _, question = EDIT_FIELDS[field]
    await query.message.reply_text(
        f"{question}\nAtual: {field_value(card, field)}\n(/cancelar para sair sem mudar)"
    )
    return EDIT_ASK


async def on_edit_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    state = context.user_data[EDITING]
    field = state["field"]
    conn = _db(context)
    before = cc.get(conn, state["card_id"])
    _, parse, _ = EDIT_FIELDS[field]
    try:
        value = parse(message.text or "")
    except FieldError as exc:
        await message.reply_text(f"Não entendi: {exc}. Tente de novo ou /cancelar.")
        return EDIT_ASK
    if (
        field == "due_day"
        and value == before.closing_day
        or field == "closing_day"
        and value == before.due_day
    ):
        await message.reply_text("Fechamento e vencimento não podem ser no mesmo dia.")
        return EDIT_ASK
    try:
        after = cc.update_field(conn, before, field, value, datetime.now(UTC))
    except cc.DuplicateCardError:
        await message.reply_text(f"Já existe um cartão chamado {value!r}.")
        return EDIT_ASK
    label = EDIT_FIELDS[field][0]
    await message.reply_text(
        f"✅ {label}: {field_value(before, field)} → {field_value(after, field)}"
    )
    return await _show_menu(message, context)


async def on_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    state = context.user_data.pop(EDITING, None) or {}
    conn = _db(context)
    if state.get("card_id"):
        card = cc.get(conn, state["card_id"])
        await query.message.reply_text(format_card_details(conn, card, _today(context)))
    return ConversationHandler.END


async def edit_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Use os botões, ou /cancelar para sair.")
    state = context.user_data.get(EDITING) or {}
    return MENU if state.get("card_id") else PICK


def build_edit_card_conversation() -> ConversationHandler:
    text = filters.TEXT & ~filters.COMMAND
    return _quiet_conversation(
        entry_points=[CommandHandler("editarcartao", edit_card)],
        states={
            PICK: [
                CallbackQueryHandler(on_pick, pattern=f"^{EDIT_CB}card:\\d+$"),
                MessageHandler(text, edit_hint),
            ],
            MENU: [
                CallbackQueryHandler(on_field, pattern=f"^{EDIT_CB}field:"),
                CallbackQueryHandler(on_done, pattern=f"^{EDIT_CB}done$"),
                MessageHandler(text, edit_hint),
            ],
            EDIT_ASK: [MessageHandler(text, on_edit_answer)],
        },
        fallbacks=[CommandHandler("cancelar", cancel)],
        allow_reentry=True,
    )
