"""Handlers de pagamento de dívida: texto livre, /pagar e /saldo.

O texto livre passa primeiro por aqui (veja handlers.register): se a mensagem
for "paguei X da dívida Y", vira pagamento; senão segue como gasto comum.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from finbot.bot.debts import card_hint, describe_projection
from finbot.bot.texts import format_alert
from finbot.config import Settings
from finbot.money import format_brl
from finbot.parsing.payment import (
    PaymentArgsError,
    parse_name_and_amount,
    parse_name_and_balance,
    parse_payment_intent,
)
from finbot.services import budgets, debts, transactions
from finbot.services.debts import Debt, PaymentResult

CB = "pagto:"  # prefixo dos botões "qual dívida?"
PENDING = "pending_payment"  # pagamento aguardando a escolha da dívida (user_data)


def _db(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    return context.bot_data["db"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


# --- textos -----------------------------------------------------------------


def format_payment(result: PaymentResult, today: date) -> str:
    d = result.debt
    lines = [f"✅ Pagamento de {format_brl(result.amount_cents)} em {d.name}"]
    if result.interest_cents:
        amortization = result.amortization_cents
        if amortization >= 0:
            lines.append(
                f"Juros do mês: {format_brl(result.interest_cents)} · "
                f"amortização: {format_brl(amortization)}"
            )
        else:
            lines.append(
                f"🚨 O pagamento não cobriu os juros do mês ({format_brl(result.interest_cents)}): "
                f"o saldo SUBIU {format_brl(-amortization)}."
            )
    before = format_brl(result.balance_before_cents)
    lines.append(f"Saldo: {before} → {format_brl(result.balance_after_cents)}")
    if d.kind == "cartao":
        # Dívida antiga do tipo cartão (de antes do módulo de cartões).
        lines.append("Cartões agora têm cadastro próprio: /novocartao (e /removerdivida esta).")
    elif result.paid_off:
        lines.append("🎉 Dívida quitada!")
        if result.overpaid_cents:
            lines.append(
                f"O pagamento passou {format_brl(result.overpaid_cents)} do saldo calculado "
                "(normal: o banco calcula os juros por dia). Registrei o valor que você pagou."
            )
    else:
        if d.remaining_installments is not None:
            lines.append(f"Parcelas restantes: {d.remaining_installments}")
        lines.append(
            "📈 "
            + describe_projection(d.balance_cents, d.rate_ppm, d.payment_cents, d.due_day, today)
        )
    lines.append(f'Também registrado como gasto em "{debts.DEBT_CATEGORY}". Errou? /desfazer')
    return "\n".join(lines)


def choice_keyboard(options: list[Debt]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(d.name, callback_data=f"{CB}{d.id}")] for d in options]
    rows.append([InlineKeyboardButton("Não, é um gasto comum", callback_data=f"{CB}gasto")])
    return InlineKeyboardMarkup(rows)


# --- ações ------------------------------------------------------------------


async def record_payment(
    message: Message, context: ContextTypes.DEFAULT_TYPE, debt: Debt, amount_cents: int
) -> None:
    tz = _settings(context).timezone
    conn = _db(context)
    result = debts.pay(conn, debt, amount_cents, datetime.now(UTC), tz)
    text = format_payment(result, datetime.now(tz).date())
    alert = budgets.check_alert(conn, transactions.get(conn, result.transaction_id), tz)
    if alert:
        text += "\n\n" + format_alert(alert)
    await message.reply_text(text)


async def try_payment(message: Message, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Se o texto é pagamento de uma dívida, trata e devolve True. Senão, False.

    Pergunta com botões quando há dúvida: mais de uma dívida combina, ou a
    mensagem tem palavras que não são da dívida ("paguei 100 de gasolina do carro").
    """
    intent = parse_payment_intent(text)
    if intent is None:
        return False
    matches = debts.match_payment(_db(context), intent.words)
    if not matches:
        return False
    if len(matches) == 1 and not debts.unexplained_words(matches[0], intent.words):
        await record_payment(message, context, matches[0], intent.amount_cents)
        return True

    context.user_data[PENDING] = {"amount": intent.amount_cents, "text": text}
    await message.reply_text(
        f"Esse pagamento de {format_brl(intent.amount_cents)} é de qual dívida?",
        reply_markup=choice_keyboard(matches),
    )
    return True


async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pagar nome valor."""
    message = update.effective_message
    try:
        name, amount = parse_name_and_amount(list(context.args or []))
    except PaymentArgsError as exc:
        await message.reply_text(f"Não entendi: {exc}.\nEx.: /pagar nubank 450")
        return
    debt = await _find_one(message, context, name)
    if debt:
        await record_payment(message, context, debt, amount)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/saldo nome valor: corrige o saldo pelo valor que aparece no app do banco."""
    message = update.effective_message
    try:
        name, balance = parse_name_and_balance(list(context.args or []))
    except PaymentArgsError as exc:
        await message.reply_text(f"Não entendi: {exc}.\nEx.: /saldo nubank 4.812,37")
        return
    debt = await _find_one(message, context, name)
    if debt is None:
        return
    old = debt.balance_cents
    updated, diff = debts.set_balance(_db(context), debt, balance, datetime.now(UTC))
    sign = "+" if diff > 0 else "-" if diff < 0 else ""
    lines = [
        f"✏️ Saldo de {updated.name} atualizado: {format_brl(old)} → {format_brl(balance)}"
        f" (diferença {sign}{format_brl(abs(diff))})"
    ]
    if updated.status == "quitada":
        lines.append("🎉 Dívida quitada!")
    else:
        today = datetime.now(_settings(context).timezone).date()
        lines.append(
            "📈 "
            + describe_projection(
                updated.balance_cents,
                updated.rate_ppm,
                updated.payment_cents,
                updated.due_day,
                today,
            )
        )
    await message.reply_text("\n".join(lines))


async def _find_one(message: Message, context: ContextTypes.DEFAULT_TYPE, name: str) -> Debt | None:
    found = debts.find(_db(context), name)
    if len(found) == 1:
        return found[0]
    if not found:
        names = ", ".join(d.name for d in debts.list_active(_db(context))) or "nenhuma"
        hint = card_hint(_db(context), name)
        await message.reply_text(f"Não encontrei a dívida {name!r}. Suas dívidas: {names}.{hint}")
    else:
        names = ", ".join(d.name for d in found)
        await message.reply_text(
            f"Mais de uma dívida combina com {name!r}: {names}. Seja mais específico."
        )
    return None
