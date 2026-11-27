"""/editardivida: muda os dados de uma dívida já cadastrada.

Fluxo: escolher a dívida -> menu com cada campo e o valor atual -> tocar num
campo -> responder (mesmas perguntas e validações do cadastro) -> a mudança é
salva na hora e o menu volta, até tocar em ✅ Concluir (ou /cancelar).

O SALDO não aparece no menu: ele muda só por pagamento, /saldo ou fatura, que
ficam registrados no histórico (e no gráfico).
"""

from __future__ import annotations

import sqlite3
import warnings
from datetime import date, datetime

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
from finbot.bot import flows
from finbot.bot.debts import (
    EDITING,
    card_hint,
    format_rate,
    kind_keyboard,
    projection_text,
)
from finbot.config import Settings
from finbot.money import format_brl
from finbot.parsing.debt import FieldError, is_skip
from finbot.services import debts
from finbot.services.debts import Debt

PICK, MENU, ASK = range(3)
CB = "editar:"

FIELD_LABELS = {
    "name": "Nome",
    "kind": "Tipo",
    "creditor": "Credor",
    "rate_ppm": "Taxa",
    "remaining_installments": "Parcelas restantes",
    "payment_cents": "Parcela",
    "due_day": "Vencimento",
    "start_date": "Início",
}
# Texto do botão de apagar o valor, por campo (o que "em branco" significa).
CLEAR_LABELS = {"rate_ppm": "Sem juros ⏭", "payment_cents": "Deixar em branco ⏭"}


def _db(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    return context.bot_data["db"]


def _today(context: ContextTypes.DEFAULT_TYPE) -> date:
    settings: Settings = context.bot_data["settings"]
    return datetime.now(settings.timezone).date()


# --- textos (puros) ---------------------------------------------------------


def editable_fields(d: Debt) -> list[str]:
    fields = list(FIELD_LABELS)
    if d.kind in debts.NO_INSTALLMENT_KINDS:
        fields.remove("remaining_installments")
    return fields


def field_value(d: Debt, field: str) -> str:
    """O valor atual, como aparece no menu e nas confirmações."""
    if field == "name":
        return d.name
    if field == "kind":
        return d.kind_label
    if field == "creditor":
        return d.creditor or "-"
    if field == "rate_ppm":
        return format_rate(d.rate_ppm)
    if field == "remaining_installments":
        return "-" if d.remaining_installments is None else str(d.remaining_installments)
    if field == "payment_cents":
        return format_brl(d.payment_cents) if d.payment_cents else "-"
    if field == "due_day":
        return f"dia {d.due_day}" if d.due_day else "-"
    if field == "start_date":
        return f"{d.start_date:%d/%m/%Y}" if d.start_date else "-"
    raise ValueError(field)


def menu_text(d: Debt) -> str:
    return (
        f"✏️ Editando: {d.name}\n"
        f"Saldo: {format_brl(d.balance_cents)} "
        "(para corrigir, use /saldo, que fica no histórico)\n\n"
        "Toque no que quer mudar:"
    )


def menu_keyboard(d: Debt) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{FIELD_LABELS[f]}: {field_value(d, f)}", callback_data=f"{CB}field:{f}"
            )
        ]
        for f in editable_fields(d)
    ]
    rows.append([InlineKeyboardButton("✅ Concluir", callback_data=f"{CB}done")])
    return InlineKeyboardMarkup(rows)


def draft_from(d: Debt) -> form.Draft:
    """A dívida no formato do rascunho do cadastro (para reusar perguntas e regras)."""
    return {
        "name": d.name,
        "kind": d.kind,
        "creditor": d.creditor,
        "balance_cents": d.balance_cents,
        "rate_ppm": d.rate_ppm,
        "remaining_installments": d.remaining_installments,
        "payment_cents": d.payment_cents,
        "due_day": d.due_day,
        "start_date": d.start_date,
    }


def change_text(before: Debt, after: Debt, field: str, today: date) -> str:
    old, new = field_value(before, field), field_value(after, field)
    lines = [f"✅ {FIELD_LABELS[field]}: {old} → {new}"]
    if field == "kind" and after.kind in debts.NO_INSTALLMENT_KINDS:
        if before.remaining_installments is not None:
            lines.append("(parcelas restantes apagadas: esse tipo não tem parcelas)")
    lines.append("📈 " + projection_text(after, today))
    return "\n".join(lines)


# --- conversa ---------------------------------------------------------------


def _state(context) -> dict:
    return context.user_data[EDITING]


def _current(context) -> Debt:
    return debts.get(_db(context), _state(context)["debt_id"])


async def _show_menu(message: Message, context) -> int:
    d = _current(context)
    await message.reply_text(menu_text(d), reply_markup=menu_keyboard(d))
    return MENU


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/editardivida [nome]."""
    message = update.effective_message
    if flows.busy(context.user_data, EDITING):
        await message.reply_text(
            "Há um cadastro de dívida em andamento. Termine ou mande /cancelar antes."
        )
        return ConversationHandler.END
    conn = _db(context)
    query = " ".join(context.args or [])
    found = debts.find(conn, query) if query else debts.list_active(conn)
    if not found:
        names = ", ".join(d.name for d in debts.list_active(conn)) or "nenhuma"
        text = f"Não encontrei a dívida {query!r}. " if query else ""
        hint = card_hint(conn, query) if query else ""
        await message.reply_text(f"{text}Dívidas ativas: {names}.{hint}")
        return ConversationHandler.END
    if len(found) == 1:
        context.user_data[EDITING] = {"debt_id": found[0].id}
        return await _show_menu(message, context)
    context.user_data[EDITING] = {"debt_id": None}
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(d.name, callback_data=f"{CB}debt:{d.id}")] for d in found]
    )
    await message.reply_text("Qual dívida você quer editar?", reply_markup=keyboard)
    return PICK


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    debt_id = int(query.data.removeprefix(f"{CB}debt:"))
    if not any(d.id == debt_id for d in debts.list_active(_db(context))):
        context.user_data.pop(EDITING, None)
        await query.message.reply_text("Essa dívida não está mais ativa.")
        return ConversationHandler.END
    context.user_data[EDITING] = {"debt_id": debt_id}
    return await _show_menu(query.message, context)


async def on_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Tocou num campo do menu: faz a pergunta desse campo."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    field = query.data.removeprefix(f"{CB}field:")
    d = _current(context)
    if field not in editable_fields(d):
        return await _show_menu(query.message, context)
    _state(context)["field"] = field

    step = form.step(field)
    text = f"{step.question(draft_from(d))}\nAtual: {field_value(d, field)}"
    if field == "kind":
        markup = kind_keyboard(CB)
    elif step.optional:
        label = CLEAR_LABELS.get(field, "Apagar valor ⏭")
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"{CB}clear")]])
    else:
        markup = None
    text += "\n(/cancelar para voltar sem mudar)"
    await query.message.reply_text(text, reply_markup=markup)
    return ASK


async def _save(message: Message, context, value) -> int:
    """Grava o campo da pergunta atual e volta ao menu."""
    field = _state(context)["field"]
    before = _current(context)
    if value is None:  # pediu para apagar: mesma regra do cadastro (ex.: parcela Price)
        draft = draft_from(before)
        form.apply(draft, form.step(field), None)
        value = draft[field]
    try:
        after = debts.update_field(_db(context), before, field, value)
    except debts.DuplicateDebtError:
        await message.reply_text(f"Já existe uma dívida chamada {value!r}. Escolha outro nome.")
        return ASK
    except ValueError as exc:
        await message.reply_text(f"Não mudei: {exc}.")
        return await _show_menu(message, context)
    await message.reply_text(change_text(before, after, field, _today(context)))
    return await _show_menu(message, context)


async def on_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    field = _state(context)["field"]
    step = form.step(field)
    text = message.text or ""
    if step.optional and is_skip(text):
        return await _save(message, context, None)
    try:
        value = step.parse(text, _today(context))
    except FieldError as exc:
        await message.reply_text(f"Não entendi: {exc}. Tente de novo ou /cancelar.")
        return ASK
    return await _save(message, context, value)


async def on_answer_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Botão de tipo ou de apagar valor, na pergunta atual."""
    query = update.callback_query
    field = _state(context).get("field")
    action = query.data.removeprefix(CB)
    if action == "clear" and field and form.step(field).optional:
        value = None
    elif action.startswith("kind:") and field == "kind":
        value = action.removeprefix("kind:")
    else:
        await query.answer("Esse botão é de uma pergunta anterior.")
        return ASK
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    return await _save(query.message, context, value)


async def on_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    d = _current(context)
    context.user_data.pop(EDITING, None)
    await query.message.reply_text(f"Pronto. Veja como ficou: /divida {d.name}")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancelar no meio da edição. O que já foi salvo continua salvo."""
    state = context.user_data.pop(EDITING, None)
    text = "Edição encerrada."
    if state and state.get("field"):
        text = (
            "Edição encerrada. A última pergunta ficou sem mudança; o que já tinha salvo continua."
        )
    await update.effective_message.reply_text(text)
    return ConversationHandler.END


async def hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Use os botões, ou /cancelar para sair da edição.")
    state = context.user_data.get(EDITING) or {}
    return MENU if state.get("debt_id") else PICK


def build_conversation() -> ConversationHandler:
    text = filters.TEXT & ~filters.COMMAND
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*per_message=False.*")
        return ConversationHandler(
            entry_points=[CommandHandler("editardivida", start)],
            states={
                PICK: [
                    CallbackQueryHandler(on_pick, pattern=f"^{CB}debt:\\d+$"),
                    MessageHandler(text, hint),
                ],
                MENU: [
                    CallbackQueryHandler(on_field, pattern=f"^{CB}field:"),
                    CallbackQueryHandler(on_done, pattern=f"^{CB}done$"),
                    MessageHandler(text, hint),
                ],
                ASK: [
                    MessageHandler(text, on_answer),
                    CallbackQueryHandler(on_answer_button, pattern=f"^{CB}(clear|kind:.+)$"),
                ],
            },
            fallbacks=[CommandHandler("cancelar", cancel)],
            allow_reentry=True,
        )
