"""Lembretes de vencimento: quais dívidas avisar hoje.

Regras:
- Avisa quando faltam de 0 a REMINDER_DAYS_BEFORE dias (padrão 3) para o
  vencimento. A janela (e não só "exatamente 3 dias") garante que, se o bot
  estava desligado no dia certo, o aviso sai assim que ele ligar, ainda antes
  de vencer.
- Cada vencimento é avisado UMA vez (tabela debt_reminders).
- O vencimento vem do dia cadastrado. Se já houve pagamento nos 25 dias antes
  do vencimento, consideramos a parcela paga e não avisamos.
- Dívida sem dia de vencimento ou com saldo zero não gera aviso.
- As faturas dos CARTÕES têm lembretes próprios (services/credit_cards.py).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from finbot.services import debts, finance
from finbot.services.debts import Debt
from finbot.timeutil import from_db, to_db

PAID_WINDOW_DAYS = 25


@dataclass(frozen=True)
class Reminder:
    debt: Debt
    due_date: date
    amount_cents: int | None  # parcela; None se não há valor fixo
    already_sent: bool = False


def _paid_recently(conn: sqlite3.Connection, debt_id: int, due: date, tz: ZoneInfo) -> bool:
    rows = conn.execute(
        "SELECT occurred_at FROM debt_events WHERE debt_id = ? AND kind = 'pagamento'",
        (debt_id,),
    ).fetchall()
    start = due - timedelta(days=PAID_WINDOW_DAYS)
    return any(start <= from_db(r["occurred_at"]).astimezone(tz).date() <= due for r in rows)


def _sent(conn: sqlite3.Connection, debt_id: int, due: date) -> bool:
    row = conn.execute(
        "SELECT 1 FROM debt_reminders WHERE debt_id = ? AND due_date = ?",
        (debt_id, due.isoformat()),
    ).fetchone()
    return row is not None


def next_due(conn: sqlite3.Connection, d: Debt, today: date, tz: ZoneInfo) -> Reminder | None:
    """O próximo vencimento que ainda precisa ser pago, ou None."""
    if d.due_day is None or d.balance_cents == 0:
        return None
    due = finance.next_due_date(today, d.due_day)
    if _paid_recently(conn, d.id, due, tz):
        return None
    amount = min(d.payment_cents, d.balance_cents) if d.payment_cents else None
    return Reminder(d, due, amount, _sent(conn, d.id, due))


def upcoming(conn: sqlite3.Connection, today: date, tz: ZoneInfo, days: int = 31) -> list[Reminder]:
    """Vencimentos em aberto nos próximos `days` dias, do mais próximo ao mais distante."""
    found = [r for d in debts.list_active(conn) if (r := next_due(conn, d, today, tz))]
    return sorted((r for r in found if (r.due_date - today).days <= days), key=lambda r: r.due_date)


def pending(
    conn: sqlite3.Connection, today: date, tz: ZoneInfo, days_before: int
) -> list[Reminder]:
    """O que avisar agora: vence em até `days_before` dias e ainda não foi avisado."""
    return [
        r
        for r in upcoming(conn, today, tz, days_before)
        if not r.already_sent and (r.due_date - today).days >= 0
    ]


def mark_sent(conn: sqlite3.Connection, debt_id: int, due: date, now: datetime) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO debt_reminders (debt_id, due_date, sent_at) VALUES (?, ?, ?)",
            (debt_id, due.isoformat(), to_db(now)),
        )
