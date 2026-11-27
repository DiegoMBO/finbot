"""Tela de Cartões: limite, faturas (atual e próximas, empilhadas por cartão),
melhor cartão para comprar hoje, pagar fatura e informar o valor real do banco.

As contas são as do bot (services/credit_cards.py, card_math.best_card)."""

from __future__ import annotations

import sqlite3
from datetime import date

from finbot.parsing.period import MONTH_NAMES
from finbot.services import card_math as cm
from finbot.services import credit_cards as cc
from finbot.services import finance

# Cor fixa por cartão (paleta categórica validada, na ordem): a cor segue o
# cartão, não a posição no gráfico. Mais de 8 cartões: as cores se repetem.
SLOTS = 8


def _ref(reference_month: str) -> str:
    y, m = (int(x) for x in reference_month.split("-"))
    return f"{MONTH_NAMES[m - 1][:3]}/{y}"


def cards_page(conn: sqlite3.Connection, today: date) -> dict:
    cards = cc.list_cards(conn)
    items = []
    months: dict[str, dict[int, int]] = {}  # AAAA-MM -> {card_id: a pagar}
    for slot, card in enumerate(cards):
        current = cc.current_invoice(conn, card, today)
        overdue = cc.overdue_invoices(conn, card, today)
        upcoming = []
        for inv in cc.invoices(conn, card.id):
            if inv.due_date < today or inv.rolled_at or inv.remaining_cents <= 0:
                continue
            months.setdefault(inv.reference_month, {})[card.id] = inv.remaining_cents
            upcoming.append(
                {"ref": _ref(inv.reference_month), "due": inv.due_date,
                 "total": inv.total_cents, "remaining": inv.remaining_cents,
                 "informed": inv.informed_total_cents is not None}
            )  # fmt: skip
        # Próximos vencimentos (para informar o valor real de uma fatura futura)
        cycle_due = cc.payable_cycle(card, today).due
        dues = []
        for k in range(6):
            y, m = finance.add_months(cycle_due.year, cycle_due.month, k)
            dues.append(finance.due_date(y, m, card.due_day))
        items.append(
            {
                "id": card.id,
                "slot": slot % SLOTS + 1,
                "name": card.name,
                "closing": card.closing_day,
                "due_day": card.due_day,
                "limit": card.limit_cents,
                "used": card.used_cents,
                "available": card.available_cents,
                "usage_pct": card.usage_bp // 100,
                "current": current,
                "overdue": sum(i.remaining_cents for i in overdue),
                "upcoming": upcoming[:6],
                "unallocated": cc.unallocated_cents(conn, card),
                "rate": card.rate_ppm,
                "dues": dues,
            }
        )
    best = cm.best_card([(c.name, c.closing_day, c.due_day) for c in cards], today)
    keys = sorted(months)[:8]
    chart = {
        "months": [_ref(k) for k in keys],
        "series": [
            {
                "name": c["name"],
                "slot": c["slot"],
                "values": [months[k].get(c["id"], 0) for k in keys],
            }
            for c in items
        ],  # fmt: skip
    }
    return {
        "cards": items,
        "best": best,
        "chart": chart,
        "total_used": sum(c["used"] for c in items),
        "total_limit": sum(c["limit"] for c in items),
        "total_current": sum(c["current"].remaining_cents for c in items if c["current"]),
        "total_unallocated": sum(c["unallocated"] for c in items),
        "today": today,
    }
