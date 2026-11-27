"""Operações no cartão pelo Telegram: compras, pagamento da fatura, /parcelado,
/estorno, desfazer, rotativo automático e lembretes de fatura."""

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

from finbot.bot import flows
from finbot.bot.card_views import due_text, invoice_label, month_short
from finbot.bot.texts import format_alert
from finbot.config import Settings
from finbot.money import format_brl, format_percent
from finbot.parsing import debt as p
from finbot.parsing.card_text import CardMessage, parse_card_message
from finbot.parsing.categories import guess_category
from finbot.parsing.debt import FieldError, is_skip
from finbot.parsing.message import EDGE_PUNCT
from finbot.parsing.text import normalize
from finbot.services import budgets, transactions
from finbot.services import card_math as cm
from finbot.services import credit_cards as cc
from finbot.services.credit_cards import Card
from finbot.services.finance import annual_equivalent_ppm

CB = "cartaoop:"
PENDING = "card_pending"  # mensagem ambígua aguardando os botões


def _db(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    return context.bot_data["db"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


def _today(context: ContextTypes.DEFAULT_TYPE) -> date:
    return datetime.now(_settings(context).timezone).date()


# --- textos (puros) ---------------------------------------------------------


def used_line(card: Card, before: int) -> str:
    return (
        f"Usado: {format_brl(before)} → {format_brl(card.used_cents)} "
        f"(disponível {format_brl(card.available_cents)})"
    )


def usage_alert_text(threshold: int | None, card: Card) -> str | None:
    if threshold is None:
        return None
    if threshold >= 100:
        return (
            f"🚨 Passou do limite do {card.name}: usado {format_brl(card.used_cents)} de "
            f"{format_brl(card.limit_cents)}. Confira se a compra foi aprovada."
        )
    return f"⚠️ Você passou de {threshold}% do limite do {card.name}."


def format_purchase(r: cc.PurchaseResult) -> str:
    pur = r.purchase
    what = f"{format_brl(pur.total_cents)} em {pur.category}"
    if pur.description:
        what += f" ({pur.description})"
    lines = [f"💳 Compra no {r.card.name}: {what}"]
    if pur.installments > 1:
        part = r.allocations[0].amount_cents
        lines[0] += f", {pur.installments}x de {format_brl(part)}"
        shown = r.allocations[:4]
        text = " · ".join(
            f"{month_short(a.invoice.reference_month)} +{format_brl(a.amount_cents)[3:]}"
            for a in shown
        )
        if len(r.allocations) > 4:
            text += f" · … até {month_short(r.allocations[-1].invoice.reference_month)}"
        lines.append(f"Faturas: {text}")
        if pur.cash_price_cents:
            interest = pur.total_cents - pur.cash_price_cents
            if interest > 0:
                rate = cm.implicit_rate_ppm(pur.cash_price_cents, part, pur.installments)
                lines.append(
                    f"Juros embutidos: {format_brl(interest)} sobre "
                    f"{format_brl(pur.cash_price_cents)} "
                    f"à vista (~{format_percent(rate)} a.m.)"
                )
    else:
        inv = r.allocations[0].invoice
        lines.append(f"Fatura: {month_short(inv.reference_month)} (vence {inv.due_date:%d/%m})")
    lines.append(used_line(r.card, r.used_before_cents))
    alert = usage_alert_text(r.usage_alert, r.card)
    if alert:
        lines.append(alert)
    lines.append("Errou? /desfazer")
    return "\n".join(lines)


def revolving_text(amount_cents: int, rate_ppm: int) -> str:
    if rate_ppm == 0:
        return "Taxa do rotativo não cadastrada: não consigo calcular os juros (/editarcartao)."
    interest, total = cm.revolving(amount_cents, rate_ppm)
    annual = format_percent(annual_equivalent_ppm(rate_ppm))
    return (
        f"Rotativo: {format_percent(rate_ppm)} ao mês ({annual} ao ano). Em 1 mês "
        f"{format_brl(amount_cents)} viram {format_brl(total)} (+{format_brl(interest)})."
    )


def format_payment(r: cc.PaymentResult, today: date) -> str:
    lines = [f"✅ Pagamento de {format_brl(r.amount_cents)} no {r.card.name}"]
    for a in r.allocations:
        inv = a.invoice
        status = (
            "paga ✅" if inv.remaining_cents == 0 else f"falta {format_brl(inv.remaining_cents)}"
        )
        lines.append(
            f"{invoice_label(inv).capitalize()}: {format_brl(a.amount_cents)} de "
            f"{format_brl(inv.total_cents)} · {status}"
        )
    if r.beyond_invoices_cents:
        lines.append(
            f"{format_brl(r.beyond_invoices_cents)} não couberam em nenhuma fatura lançada: "
            'abateram as parcelas "sem mês definido".'
        )
    lines.append(used_line(r.card, r.used_before_cents))
    # Alerta de rotativo: a fatura a pagar (a mais antiga que recebeu dinheiro) ficou incompleta.
    due_now = [
        a.invoice
        for a in r.allocations
        if a.invoice.due_date <= cm.payable_cycle(today, r.card.closing_day, r.card.due_day).due
    ]
    pending = [inv for inv in due_now if inv.remaining_cents > 0]
    if pending:
        inv = pending[0]
        when = (
            f"Se não pagar até {inv.due_date:%d/%m}, esse valor vai para o rotativo."
            if inv.due_date >= today
            else "A fatura já venceu: amanhã esse valor vai para o rotativo."
        )
        lines += [
            "",
            "🚨🚨 ATENÇÃO: ROTATIVO 🚨🚨",
            f"Faltam {format_brl(inv.remaining_cents)} da {invoice_label(inv)}. {when}",
            revolving_text(inv.remaining_cents, r.card.rate_ppm),
        ]
    lines.append("(Pagar a fatura não entra como gasto: as compras já foram contadas.) /desfazer")
    return "\n".join(lines)


def format_roll(r: cc.RollResult) -> str:
    return (
        f"🚨 Rotativo no {r.card.name}: a {invoice_label(r.invoice)} venceu faltando "
        f"{format_brl(r.unpaid_cents)}.\n"
        f"Lancei {format_brl(r.unpaid_cents + r.interest_cents)} "
        f"na {invoice_label(r.next_invoice)} "
        f"({format_brl(r.unpaid_cents)} + {format_brl(r.interest_cents)} de juros"
        + (
            f" a {format_percent(r.card.rate_ppm)} a.m."
            if r.card.rate_ppm
            else ", taxa não cadastrada"
        )
        + ").\nQuando o banco fechar a fatura, informe o valor real: "
        f"/fatura {r.card.name} valor"
    )


def format_invoice_reminder(r: cc.InvoiceReminder, today: date) -> str:
    inv = r.invoice
    return (
        f"⏰ Lembrete: a {invoice_label(inv)} do {r.card.name} {due_text(inv.due_date, today)}.\n"
        f"Falta pagar: {format_brl(inv.remaining_cents)}. Pagar menos que o total leva o resto "
        "para o rotativo.\n"
        f"Já pagou? Registre com: paguei {format_brl(inv.remaining_cents)[3:]} "
        f"da fatura do {r.card.name}"
    )


def choice_keyboard(card: Card) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🛒 Compra no {card.name}", callback_data=f"{CB}compra")],
            [
                InlineKeyboardButton(
                    f"💸 Pagamento da fatura {card.name}", callback_data=f"{CB}pagar"
                )
            ],
            [InlineKeyboardButton("Gasto comum (sem cartão)", callback_data=f"{CB}gasto")],
        ]
    )


# --- ações ------------------------------------------------------------------


async def _budget_alert(conn, purchase_id: int, tz) -> str | None:
    row = conn.execute(
        "SELECT transaction_id FROM invoice_items"
        " WHERE purchase_id = ? AND transaction_id IS NOT NULL"
        " ORDER BY installment_number LIMIT 1",
        (purchase_id,),
    ).fetchone()
    if row is None:
        return None
    alert = budgets.check_alert(conn, transactions.get(conn, row["transaction_id"]), tz)
    return format_alert(alert) if alert else None


async def record_purchase(message: Message, context, card: Card, cm_msg: CardMessage) -> None:
    conn = _db(context)
    tz = _settings(context).timezone
    now = datetime.now(UTC)
    entry = cm_msg.entry
    r = cc.add_purchase(
        conn,
        card,
        description=entry.description,
        category=entry.category,
        purchased_on=now.astimezone(tz).date(),
        now=now,
        tz=tz,
        total_cents=entry.amount_cents,
        installments=cm_msg.installments,
    )
    text = format_purchase(r)
    alert = await _budget_alert(conn, r.purchase.id, tz)
    if alert:
        text += "\n\n" + alert
    await message.reply_text(text)


async def record_payment(message: Message, context, card: Card, amount_cents: int) -> None:
    conn = _db(context)
    today = _today(context)
    r = cc.pay(conn, card, amount_cents, today, datetime.now(UTC))
    await message.reply_text(format_payment(r, today))


def _tokens(text: str) -> list[str]:
    return [normalize(t.strip(EDGE_PUNCT)) for t in text.split() if t.strip(EDGE_PUNCT)]


async def try_card(message: Message, context, text: str) -> bool:
    """Se o texto cita um cartão, trata como compra/pagamento e devolve True."""
    conn = _db(context)
    cards = cc.mentioned_in(conn, _tokens(text))
    if not cards:
        return False
    # "mercado pago" e "mercado": vale o nome mais longo que bateu.
    longest = max(len(cc.name_key(c.name).split()) for c in cards)
    cards = [c for c in cards if len(cc.name_key(c.name).split()) == longest]
    if len(cards) > 1:
        names = ", ".join(c.name for c in cards)
        await message.reply_text(f"A mensagem cita mais de um cartão ({names}). Escreva só um.")
        return True
    card = cards[0]
    parsed = parse_card_message(text, set(cc.name_key(card.name).split()))
    if parsed is None:
        return False
    if parsed.intent == "compra":
        await record_purchase(message, context, card, parsed)
    elif parsed.intent == "pagamento":
        await record_payment(message, context, card, parsed.entry.amount_cents)
    else:
        context.user_data[PENDING] = {"card_id": card.id, "text": text}
        await message.reply_text(
            f"{format_brl(parsed.entry.amount_cents)}: foi uma compra no {card.name} ou o "
            "pagamento da fatura?",
            reply_markup=choice_keyboard(card),
        )
    return True


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Botões da mensagem ambígua. Devolve o texto se a escolha foi "gasto comum"
    (quem chama registra como gasto normal)."""
    query = update.callback_query
    pending = context.user_data.pop(PENDING, None)
    if pending is None:
        await query.answer("Esse botão expirou.")
        return None
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    choice = query.data.removeprefix(CB)
    conn = _db(context)
    card = cc.get(conn, pending["card_id"])
    if choice == "gasto":
        # Gasto comum: tira o nome do cartão do texto, senão ele vira descrição.
        card_words = set(cc.name_key(card.name).split())
        return " ".join(w for w in pending["text"].split() if normalize(w) not in card_words)
    parsed = parse_card_message(pending["text"], set(cc.name_key(card.name).split()))
    if choice == "compra":
        await record_purchase(query.message, context, card, parsed)
    else:
        await record_payment(query.message, context, card, parsed.entry.amount_cents)
    return None


# --- tarefas agendadas -------------------------------------------------------


async def run_daily(context) -> int:
    """Rotativo das faturas vencidas + lembretes de fatura. Devolve quantas mensagens mandou."""
    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    today = datetime.now(settings.timezone).date()
    sent = 0
    for card in cc.list_cards(conn):
        for roll in cc.roll_overdue(conn, card, today, datetime.now(UTC)):
            await context.bot.send_message(settings.allowed_chat_id, format_roll(roll))
            sent += 1
    for r in cc.due_reminders(conn, today, settings.reminder_days_before):
        await context.bot.send_message(settings.allowed_chat_id, format_invoice_reminder(r, today))
        cc.mark_reminded(conn, r.card.id, r.invoice.due_date, datetime.now(UTC))
        sent += 1
    return sent


# --- /parcelado ---------------------------------------------------------------

PICK, ASK, CONFIRM = range(10, 13)
DRAFT = "installment_draft"
PCB = "parcelado:"

QUESTIONS = {
    "description": ("O que você comprou? Ex.: TV", False),
    "category": ("Categoria? Ex.: compras, casa, lazer (pular = adivinho pela descrição)", True),
    "installments": ("Em quantas parcelas? (2 a 48)", False),
    "installment_cents": ("Valor de CADA parcela? Ex.: 330", False),
    "cash_price_cents": (
        "Preço À VISTA, se souber (para calcular os juros embutidos)? Ex.: 3.000",
        True,
    ),
    "purchased_on": ("Data da compra? Ex.: hoje, 25/11/2026", True),
}
ORDER = list(QUESTIONS)


def _parse_field(field: str, text: str, today: date):
    if field == "description":
        return p.parse_text(text)
    if field == "category":
        return p.parse_text(text).lower().lstrip("#")
    if field == "installments":
        n = p.parse_installments(text)
        if not 2 <= n <= 48:
            raise FieldError("use de 2 a 48 parcelas")
        return n
    if field in ("installment_cents", "cash_price_cents"):
        return p.parse_money(text)
    return p.parse_date(text, today)


def format_installment_draft(card: Card, d: dict, today: date) -> str:
    n, part = d["installments"], d["installment_cents"]
    on = d.get("purchased_on") or today
    first = cm.cycle_for_purchase(on, card.closing_day, card.due_day)
    last = cm.shift(first, n - 1, card.closing_day, card.due_day)
    lines = [
        "Confira a compra parcelada:",
        f"Cartão: {card.name}",
        f"O quê: {d['description']} · categoria: {d['category']}",
        f"{n}x de {format_brl(part)} = {format_brl(n * part)}",
        f"Data da compra: {on:%d/%m/%Y}",
        f"Faturas: de {month_short(first.reference_month)} a {month_short(last.reference_month)}",
    ]
    cash = d.get("cash_price_cents")
    if cash:
        interest = n * part - cash
        if interest > 0:
            rate = cm.implicit_rate_ppm(cash, part, n)
            lines.append(
                f"Juros embutidos: {format_brl(interest)} (~{format_percent(rate)} a.m.) "
                f"sobre {format_brl(cash)} à vista"
            )
        else:
            lines.append("Sem juros (parcelas somam o preço à vista).")
    new_used = card.used_cents + n * part
    lines.append(f"Usado: {format_brl(card.used_cents)} → {format_brl(new_used)}")
    return "\n".join(lines)


SKIP_P = InlineKeyboardMarkup([[InlineKeyboardButton("Pular ⏭", callback_data=f"{PCB}skip")]])
CONFIRM_P = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Salvar", callback_data=f"{PCB}save"),
            InlineKeyboardButton("❌ Cancelar", callback_data=f"{PCB}cancel"),
        ]
    ]
)


async def _ask_installment(message: Message, context) -> int:
    d = context.user_data[DRAFT]
    question, optional = QUESTIONS[d["_field"]]
    text = question + ("\n(opcional: Pular)" if optional else "")
    await message.reply_text(text, reply_markup=SKIP_P if optional else None)
    return ASK


async def _advance_installment(message: Message, context, value) -> int:
    d = context.user_data[DRAFT]
    field = d["_field"]
    if field == "category" and value is None:
        value = guess_category(d["description"].split(), "expense")
    d[field] = value
    idx = ORDER.index(field)
    if idx + 1 < len(ORDER):
        d["_field"] = ORDER[idx + 1]
        return await _ask_installment(message, context)
    card = cc.get(_db(context), d["card_id"])
    await message.reply_text(
        format_installment_draft(card, d, _today(context)), reply_markup=CONFIRM_P
    )
    return CONFIRM


def _busy(context) -> bool:
    return flows.busy(context.user_data)


async def installment_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if _busy(context):
        await message.reply_text("Há outro cadastro em andamento. Termine ou use /cancelar.")
        return ConversationHandler.END
    conn = _db(context)
    query = " ".join(context.args or [])
    cards = cc.find(conn, query) if query else cc.list_cards(conn)
    if not cards:
        await message.reply_text("Nenhum cartão encontrado. Cadastre com /novocartao.")
        return ConversationHandler.END
    context.user_data[DRAFT] = {"_field": ORDER[0], "card_id": None}
    if len(cards) == 1:
        context.user_data[DRAFT]["card_id"] = cards[0].id
        await message.reply_text(f"Compra parcelada no {cards[0].name}. /cancelar sai.")
        return await _ask_installment(message, context)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(c.name, callback_data=f"{PCB}card:{c.id}")] for c in cards]
    )
    await message.reply_text("Em qual cartão?", reply_markup=keyboard)
    return PICK


async def installment_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    context.user_data[DRAFT]["card_id"] = int(query.data.removeprefix(f"{PCB}card:"))
    return await _ask_installment(query.message, context)


async def installment_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    d = context.user_data[DRAFT]
    _, optional = QUESTIONS[d["_field"]]
    text = message.text or ""
    if optional and is_skip(text):
        return await _advance_installment(message, context, None)
    try:
        value = _parse_field(d["_field"], text, _today(context))
    except FieldError as exc:
        await message.reply_text(f"Não entendi: {exc}.")
        return await _ask_installment(message, context)
    return await _advance_installment(message, context, value)


async def installment_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    d = context.user_data.get(DRAFT)
    if not d or not QUESTIONS[d["_field"]][1]:
        await query.answer("Esse botão é de uma pergunta anterior.")
        return ASK
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    return await _advance_installment(query.message, context, None)


async def installment_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    d = context.user_data.pop(DRAFT, None)
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    if d is None or query.data == f"{PCB}cancel":
        await query.message.reply_text("Cancelado. Nada foi salvo.")
        return ConversationHandler.END
    conn = _db(context)
    tz = _settings(context).timezone
    now = datetime.now(UTC)
    r = cc.add_purchase(
        conn,
        cc.get(conn, d["card_id"]),
        description=d["description"],
        category=d["category"],
        purchased_on=d.get("purchased_on") or now.astimezone(tz).date(),
        now=now,
        tz=tz,
        installments=d["installments"],
        installment_cents=d["installment_cents"],
        cash_price_cents=d.get("cash_price_cents"),
    )
    await query.message.reply_text(format_purchase(r))
    return ConversationHandler.END


async def flow_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    had = context.user_data.pop(DRAFT, None) or context.user_data.pop(REFUND, None)
    await update.effective_message.reply_text(
        "Cancelado. Nada foi salvo." if had else "Não há nada para cancelar."
    )
    return ConversationHandler.END


async def flow_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Use os botões, ou /cancelar.")
    return None  # continua no mesmo estado


def _quiet(**kwargs) -> ConversationHandler:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*per_message=False.*")
        return ConversationHandler(**kwargs)


def build_installment_conversation() -> ConversationHandler:
    text = filters.TEXT & ~filters.COMMAND
    return _quiet(
        entry_points=[CommandHandler("parcelado", installment_start)],
        states={
            PICK: [
                CallbackQueryHandler(installment_pick, pattern=f"^{PCB}card:\\d+$"),
                MessageHandler(text, flow_hint),
            ],
            ASK: [
                MessageHandler(text, installment_answer),
                CallbackQueryHandler(installment_skip, pattern=f"^{PCB}skip$"),
            ],
            CONFIRM: [
                CallbackQueryHandler(installment_confirm, pattern=f"^{PCB}(save|cancel)$"),
                MessageHandler(text, flow_hint),
            ],
        },
        fallbacks=[CommandHandler("cancelar", flow_cancel)],
        allow_reentry=True,
    )


# --- /estorno -----------------------------------------------------------------

R_CARD, R_PURCHASE, R_KIND, R_AMOUNT, R_CONFIRM = range(20, 25)
REFUND = "refund_draft"
RCB = "estorno:"


def purchase_button_label(pur: cc.Purchase, refunded: int) -> str:
    label = (
        f"{pur.purchased_on:%d/%m} {format_brl(pur.total_cents)} {pur.description or pur.category}"
    )
    if pur.installments > 1:
        label += f" ({pur.installments}x)"
    if refunded:
        label += f" · já estornado {format_brl(refunded)}"
    return label[:60]


async def _show_purchases(message: Message, context) -> int:
    conn = _db(context)
    card = cc.get(conn, context.user_data[REFUND]["card_id"])
    rows = []
    for pur in cc.purchases(conn, card.id, 8):
        refunded = cc.refunded_cents(conn, pur.id)
        if refunded < pur.total_cents:
            rows.append(
                [
                    InlineKeyboardButton(
                        purchase_button_label(pur, refunded), callback_data=f"{RCB}compra:{pur.id}"
                    )
                ]
            )
    if not rows:
        context.user_data.pop(REFUND, None)
        await message.reply_text(f"Nenhuma compra para estornar no {card.name}.")
        return ConversationHandler.END
    await message.reply_text(
        f"Qual compra do {card.name} foi estornada?", reply_markup=InlineKeyboardMarkup(rows)
    )
    return R_PURCHASE


async def refund_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if _busy(context):
        await message.reply_text("Há outro cadastro em andamento. Termine ou use /cancelar.")
        return ConversationHandler.END
    conn = _db(context)
    query = " ".join(context.args or [])
    cards = cc.find(conn, query) if query else cc.list_cards(conn)
    if not cards:
        await message.reply_text("Nenhum cartão encontrado.")
        return ConversationHandler.END
    context.user_data[REFUND] = {"card_id": None}
    if len(cards) == 1:
        context.user_data[REFUND]["card_id"] = cards[0].id
        return await _show_purchases(message, context)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(c.name, callback_data=f"{RCB}card:{c.id}")] for c in cards]
    )
    await message.reply_text("Em qual cartão foi o estorno?", reply_markup=keyboard)
    return R_CARD


async def refund_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    context.user_data[REFUND]["card_id"] = int(query.data.removeprefix(f"{RCB}card:"))
    return await _show_purchases(query.message, context)


async def refund_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    pid = int(query.data.removeprefix(f"{RCB}compra:"))
    context.user_data[REFUND]["purchase_id"] = pid
    conn = _db(context)
    pur = cc.get_purchase(conn, pid)
    left = pur.total_cents - cc.refunded_cents(conn, pid)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"Total ({format_brl(left)})", callback_data=f"{RCB}total"),
                InlineKeyboardButton("Parcial", callback_data=f"{RCB}parcial"),
            ]
        ]
    )
    await query.message.reply_text("O estorno foi total ou parcial?", reply_markup=keyboard)
    return R_KIND


async def _refund_summary(message: Message, context) -> int:
    conn = _db(context)
    d = context.user_data[REFUND]
    pur = cc.get_purchase(conn, d["purchase_id"])
    left = pur.total_cents - cc.refunded_cents(conn, pur.id)
    amount = d.get("amount") or left
    kind = "TOTAL" if amount == left else "parcial"
    text = (
        f"Confirma o estorno {kind} de {format_brl(amount)} da compra "
        f'"{pur.description or pur.category}" ({format_brl(pur.total_cents)})?\n'
        + (
            "Parcelas futuras somem; o que já foi cobrado vira crédito na fatura aberta. "
            "O gasto sai do /mes."
            if amount == left
            else "O valor vira crédito na fatura aberta e é descontado do gasto no /mes."
        )
        + "\n⚠️ Estorno não pode ser desfeito com /desfazer."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirmar", callback_data=f"{RCB}ok"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"{RCB}nao"),
            ]
        ]
    )
    await message.reply_text(text, reply_markup=keyboard)
    return R_CONFIRM


async def refund_kind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    if query.data == f"{RCB}parcial":
        await query.message.reply_text("Qual o valor estornado? Ex.: 30")
        return R_AMOUNT
    context.user_data[REFUND]["amount"] = None
    return await _refund_summary(query.message, context)


async def refund_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    try:
        amount = p.parse_money(message.text or "")
    except FieldError as exc:
        await message.reply_text(f"Não entendi: {exc}.")
        return R_AMOUNT
    d = context.user_data[REFUND]
    conn = _db(context)
    pur = cc.get_purchase(conn, d["purchase_id"])
    left = pur.total_cents - cc.refunded_cents(conn, pur.id)
    if amount > left:
        await message.reply_text(f"O máximo que dá para estornar é {format_brl(left)}.")
        return R_AMOUNT
    d["amount"] = amount
    return await _refund_summary(message, context)


async def refund_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    d = context.user_data.pop(REFUND, None)
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    if d is None or query.data == f"{RCB}nao":
        await query.message.reply_text("Ok, nada foi estornado.")
        return ConversationHandler.END
    conn = _db(context)
    r = cc.refund(
        conn,
        cc.get(conn, d["card_id"]),
        cc.get_purchase(conn, d["purchase_id"]),
        d.get("amount"),
        _today(context),
        datetime.now(UTC),
    )
    lines = [f"↩️ Estorno de {format_brl(r.amount_cents)} no {r.card.name}"]
    if r.removed_future_cents:
        lines.append(f"Parcelas canceladas: {format_brl(r.removed_future_cents)}")
    if r.credit_cents:
        lines.append(
            f"Crédito de {format_brl(r.credit_cents)} na {invoice_label(r.credit_invoice)}"
        )
    lines.append(used_line(r.card, r.used_before_cents))
    await query.message.reply_text("\n".join(lines))
    return ConversationHandler.END


def build_refund_conversation() -> ConversationHandler:
    text = filters.TEXT & ~filters.COMMAND
    return _quiet(
        entry_points=[CommandHandler("estorno", refund_start)],
        states={
            R_CARD: [
                CallbackQueryHandler(refund_card, pattern=f"^{RCB}card:\\d+$"),
                MessageHandler(text, flow_hint),
            ],
            R_PURCHASE: [
                CallbackQueryHandler(refund_purchase, pattern=f"^{RCB}compra:\\d+$"),
                MessageHandler(text, flow_hint),
            ],
            R_KIND: [
                CallbackQueryHandler(refund_kind, pattern=f"^{RCB}(total|parcial)$"),
                MessageHandler(text, flow_hint),
            ],
            R_AMOUNT: [MessageHandler(text, refund_amount)],
            R_CONFIRM: [
                CallbackQueryHandler(refund_confirm, pattern=f"^{RCB}(ok|nao)$"),
                MessageHandler(text, flow_hint),
            ],
        },
        fallbacks=[CommandHandler("cancelar", flow_cancel)],
        allow_reentry=True,
    )
