"""Registrar pelo app: gasto, entrada ou compra no cartão (à vista ou parcelada).

Usa as MESMAS funções do bot (transactions.add, credit_cards.add_purchase,
budgets.check_alert): o que entra aqui aparece no bot (/mes, /extrato, /cartao)
e pode ser desfeito com /desfazer, e vice-versa.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from finbot.bot.texts import format_alert
from finbot.money import AmountError, format_brl, parse_amount
from finbot.parsing.categories import guess_category
from finbot.parsing.message import ParsedEntry
from finbot.services import budgets, transactions
from finbot.services import credit_cards as cc
from finbot.services.undo import describe

MONTH_ABBR = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")


class EntryError(ValueError):
    """Formulário com algum campo inválido (a mensagem diz qual)."""


@dataclass(frozen=True)
class EntryForm:
    kind: str  # "expense" ou "income"
    amount: str
    description: str = ""
    category: str = ""
    card_id: str = ""  # vazio = Pix/débito/dinheiro
    installments: str = "1"
    day: str = ""  # AAAA-MM-DD; vazio = hoje


def _when(form_day: str, today: date, now: datetime, tz: ZoneInfo) -> tuple[date, datetime]:
    """Data escolhida (até hoje). Hoje: a hora de agora; outro dia: meio-dia."""
    if not form_day:
        return today, now
    try:
        day = date.fromisoformat(form_day)
    except ValueError as exc:
        raise EntryError("data inválida") from exc
    if day > today:
        raise EntryError("a data não pode ser no futuro")
    if day == today:
        return day, now
    return day, datetime.combine(day, time(12), tzinfo=tz)


def register(conn: sqlite3.Connection, form: EntryForm, now: datetime, tz: ZoneInfo) -> str:
    """Grava e devolve a mensagem de confirmação (com alerta de orçamento, se houver)."""
    if form.kind not in ("expense", "income"):
        raise EntryError("escolha gasto ou entrada")
    try:
        amount = parse_amount(form.amount)
    except AmountError as exc:
        raise EntryError("valor inválido (ex.: 35 ou 12,50)") from exc
    if amount <= 0:
        raise EntryError("o valor precisa ser maior que zero")
    description = " ".join(form.description.split())[:100]
    raw_category = form.category.strip().lower()
    if raw_category:
        category = budgets.canonical_category(conn, raw_category)
    else:
        category = guess_category(description.split(), form.kind)
    today = now.astimezone(tz).date()
    day, moment = _when(form.day, today, now, tz)

    if form.card_id and form.kind == "expense":
        card = next((c for c in cc.list_cards(conn) if str(c.id) == form.card_id), None)
        if card is None:
            raise EntryError("cartão não encontrado")
        try:
            installments = int(form.installments or "1")
        except ValueError as exc:
            raise EntryError("parcelas inválidas") from exc
        if not 1 <= installments <= 48:
            raise EntryError("parcelas: de 1 a 48")
        r = cc.add_purchase(
            conn, card, description=description, category=category, purchased_on=day,
            now=now, tz=tz, total_cents=amount, installments=installments,
        )  # fmt: skip
        months = ", ".join(
            f"{MONTH_ABBR[int(a.invoice.reference_month[5:7]) - 1]}/{a.invoice.reference_month[:4]}"
            for a in r.allocations
        )
        how = f"em {installments}x" if installments > 1 else "à vista"
        text = (
            f"✅ Compra no {card.name} ({how}): {format_brl(amount)} em {category}"
            + (f" ({description})" if description else "")
            + f". Faturas: {months}. Usado: {format_brl(r.card.used_cents)} de "
            f"{format_brl(card.limit_cents)}."
        )
        if r.usage_alert:
            text += f"\n⚠️ Você passou de {r.usage_alert}% do limite do {card.name}."
        return text

    entry = ParsedEntry(form.kind, amount, category, description)
    t = transactions.add(conn, entry, moment)
    label = "Entrada registrada" if t.kind == "income" else "Gasto registrado"
    text = f"✅ {label}: {describe(t)}" + ("" if day == today else f" em {day:%d/%m}")
    alert = budgets.check_alert(conn, t, tz)
    if alert:
        text += "\n" + format_alert(alert)
    return text
