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
    return {
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
