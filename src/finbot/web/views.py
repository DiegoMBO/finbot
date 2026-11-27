"""Dados das telas, montados a partir de services/ (sem HTML aqui).

Cada função devolve dicionários/listas simples: o template mostra, o JavaScript
do gráfico recebe em JSON. Assim dá para testar os números sem navegador.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from finbot.money import format_brl
from finbot.services import cash

CHART_DAYS = 90


def brl(cents: int) -> str:
    """Com sinal: −R$ 191,97 (o format_brl usa o sinal do Python)."""
    return f"−{format_brl(-cents)}" if cents < 0 else format_brl(cents)


@dataclass(frozen=True)
class Tile:
    """Um número em destaque no topo do painel."""

    label: str
    value: str
    note: str = ""
    tone: str = "neutral"  # neutral | good | warning | critical


def _item_json(i: cash.Item) -> dict:
    sign = 1 if i.group == cash.INCOME else -1
    return {
        "label": i.label,
        "group": i.group,
        "cents": sign * i.amount_cents,
        "status": i.status,
        "note": i.note,
    }


def daily_series(conn: sqlite3.Connection, today: date, tz: ZoneInfo, extra=()) -> dict:
    """JSON do gráfico: um ponto por dia (saldo no fim do dia) + o que acontece nele."""
    points = cash.daily_balance(conn, today, tz, CHART_DAYS, extra)
    if points is None:
        return {"ready": False, "days": []}
    return {
        "ready": True,
        "days": [
            {
                "date": p.day.isoformat(),
                "cents": p.balance_cents,
                "items": [_item_json(i) for i in p.items],
            }
            for p in points
        ],
    }


def dashboard(conn: sqlite3.Connection, today: date, tz: ZoneInfo, extra=()) -> dict:
    """Tudo o que o Painel mostra, já formatado."""
    anchor = cash.get_anchor(conn)
    points = cash.daily_balance(conn, today, tz, CHART_DAYS, extra)
    if anchor is None or points is None:
        return {"ready": False, "today": today}

    today_items = points[0].items
    now = points[0].balance_cents - sum(_item_json(i)["cents"] for i in today_items)
    tiles = [Tile("Na conta hoje (estimado)", brl(now), "o que você informou + o que registrou")]

    outlook = cash.outlook(conn, today.year, today.month, today, tz, extra)
    if outlook is not None:
        until = f"até {outlook.next_income_day:%d/%m}" if outlook.next_income_day else ""
        if outlook.free_cents < 0:
            tiles.append(
                Tile(f"Faltam {until}".strip(), brl(-outlook.free_cents),
                     "para pagar as contas até o próximo dinheiro", "critical")
            )  # fmt: skip
        else:
            tone = "warning" if outlook.free_cents < 30_000 else "good"
            tiles.append(
                Tile(f"Sobram {until}".strip(), brl(outlook.free_cents),
                     "depois de pagar as contas até o próximo dinheiro", tone)
            )  # fmt: skip

    low = min(points, key=lambda p: (p.balance_cents, p.day))
    tone = (
        "critical"
        if low.balance_cents < 0
        else ("warning" if low.balance_cents < 30_000 else "good")
    )
    tiles.append(
        Tile("Ponto mais baixo", brl(low.balance_cents),
             f"em {low.day:%d/%m} (próximos {CHART_DAYS} dias)", tone)
    )  # fmt: skip

    negative = _negative_ranges(points)
    upcoming = [
        {"day": p.day, **_item_json(i)} for p in points for i in p.items if i.group != cash.INCOME
    ][:10]
    incomes = [
        {"day": p.day, **_item_json(i)} for p in points for i in p.items if i.group == cash.INCOME
    ][:5]
    s = cash.statement(conn, today.year, today.month, today, tz, extra)
    form = spending(conn, today.year, today.month, today, tz)
    return {
        "known_categories": form["known_categories"],
        "cards": form["cards"],
        "ready": True,
        "today": today,
        "anchor": anchor,
        "tiles": tiles,
        "negative": negative,
        "upcoming": upcoming,
        "incomes": incomes,
        "undated_cents": s.undated_cents,
        "undated_cards": s.undated_cards,
        "growing": s.growing,
    }


def _negative_ranges(points: list[cash.DayPoint]) -> list[tuple[date, date]]:
    ranges = []
    start = None
    for prev, p in zip([None, *points], points, strict=False):
        if p.balance_cents < 0 and start is None:
            start = p.day
        elif p.balance_cents >= 0 and start is not None:
            ranges.append((start, prev.day))
            start = None
    if start is not None:
        ranges.append((start, points[-1].day))
    return ranges


# --- Gastos ------------------------------------------------------------------------


def _month_key(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def _short_month(year: int, month: int) -> str:
    from finbot.parsing.period import MONTH_NAMES

    return f"{MONTH_NAMES[month - 1][:3]}/{year}"


def month_nav(year: int, month: int, today: date, max_ahead: int = 0) -> dict:
    """Mês anterior/seguinte para as setas (sem passar de `max_ahead` meses à frente)."""
    from finbot.parsing.period import month_label
    from finbot.services.finance import add_months

    prev = add_months(year, month, -1)
    nxt = add_months(year, month, 1)
    limit = add_months(today.year, today.month, max_ahead)
    return {
        "label": month_label(year, month),
        "prev": _month_key(*prev),
        "next": _month_key(*nxt) if nxt <= limit else None,
        "current": (year, month) == (today.year, today.month),
    }


def parse_month_key(raw: str | None, today: date) -> tuple[int, int]:
    """'2026-12' -> (2026, 12); vazio ou inválido -> mês atual."""
    try:
        year, month = (int(x) for x in (raw or "").split("-"))
        if 1 <= month <= 12 and 2000 <= year <= 2100:
            return year, month
    except ValueError:
        pass
    return today.year, today.month


def spending(conn: sqlite3.Connection, year: int, month: int, today: date, tz: ZoneInfo) -> dict:
    """Tela de Gastos: totais, por categoria, últimos 6 meses, orçamentos e registros."""
    from finbot.parsing.categories import EXPENSE_KEYWORDS
    from finbot.services import budgets, transactions
    from finbot.services import credit_cards as cc
    from finbot.services.finance import add_months

    start, end = transactions.month_bounds(year, month, tz)
    items = transactions.list_between(conn, start, end)
    summary = transactions.summarize(items)
    card_tx = cc.card_transaction_ids(conn)
    payroll_tx = {
        r["transaction_id"]
        for r in conn.execute("SELECT transaction_id FROM payroll_payments")
        if r["transaction_id"]
    }
    categories = [
        {"name": name, "cents": cents, "pct": round(100 * cents / summary.expense_cents)}
        for name, cents in summary.expenses_by_category.items()
    ]
    history = []
    for k in range(5, -1, -1):
        y, m = add_months(year, month, -k)
        s, e = transactions.month_bounds(y, m, tz)
        summ = transactions.summarize(transactions.list_between(conn, s, e))
        history.append(
            {
                "month": _short_month(y, m),
                "expense": summ.expense_cents,
                "income": summ.income_cents,
            }
        )
    records = []
    for t in reversed(items):
        if t.id in card_tx:
            origin = "cartão"
        elif t.id in payroll_tx:
            origin = "salário (lançado pelo bot)"
        elif t.category == "dívidas":
            origin = "pagamento de dívida"
        else:
            origin = ""
        records.append(
            {
                "day": t.created_at.astimezone(tz).date(),
                "kind": t.kind,
                "category": t.category,
                "description": t.description,
                "cents": t.amount_cents,
                "origin": origin,
            }
        )
    statuses = budgets.statuses(budgets.list_budgets(conn), items)
    known = sorted(
        set(EXPENSE_KEYWORDS)
        | {r["category"] for r in conn.execute("SELECT DISTINCT category FROM transactions")}
    )
    return {
        "nav": month_nav(year, month, today),
        "summary": summary,
        "categories": categories,
        "history": history,
        "records": records,
        "budgets": [
            {
                "category": s.category,
                "spent": s.spent_cents,
                "limit": s.limit_cents,
                "pct": s.percent,
                "remaining": s.remaining_cents,
            }
            for s in statuses
        ],  # fmt: skip
        "known_categories": known,
        "cards": [{"id": c.id, "name": c.name} for c in cc.list_cards(conn)],
        "today": today,
    }


# --- Extrato -------------------------------------------------------------------------


GROUP_TITLES = {
    cash.INCOME: "💰 Entra",
    cash.DEBT: "💸 Dívidas",
    cash.INVOICE: "💳 Faturas",
    cash.EXPENSE: "🛒 Gastos registrados (fora do cartão)",
}


def statement_page(
    conn: sqlite3.Connection, year: int, month: int, today: date, tz: ZoneInfo, extra=()
) -> dict:
    s = cash.statement(conn, year, month, today, tz, extra)
    o = cash.outlook(conn, year, month, today, tz, extra)
    groups = []
    for group, title in GROUP_TITLES.items():
        rows = [i for i in s.items if i.group == group]
        groups.append({"key": group, "title": title, "total": s.total(group), "items": rows})
    out_total = s.total(cash.DEBT) + s.total(cash.INVOICE) + s.total(cash.EXPENSE)
    return {
        "nav": month_nav(year, month, today, cash.MAX_MONTHS_AHEAD),
        "s": s,
        "o": o,
        "groups": groups,
        "income_total": s.total(cash.INCOME),
        "out_total": out_total,
        "today": today,
    }
