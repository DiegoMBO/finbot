"""Séries do gráfico de evolução de uma dívida: histórico real + projeção.

Histórico: um ponto por dia com evento (cadastro, pagamento, ajuste, fatura),
usando o saldo depois do último evento daquele dia.
Projeção: parte de hoje com o saldo atual e segue mês a mês pelo mesmo
cálculo do /dividas (services/finance.py), nas datas de vencimento.
Se a dívida está crescendo, mostramos 24 meses de crescimento.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from finbot.services import finance
from finbot.services.debts import REVOLVING, Debt, DebtEvent

MAX_PROJECTION_MONTHS = 120  # 10 anos no eixo; depois disso o gráfico fica ilegível
GROWTH_MONTHS = 24

Point = tuple[date, int]  # (data, saldo em centavos)


@dataclass(frozen=True)
class Evolution:
    history: list[Point]
    projection: list[Point]
    status: str  # o status da projeção (finance.Status) ou "em_dia"
    payoff: date | None  # data prevista de quitação, quando existe
    truncated: bool  # projeção cortada em MAX_PROJECTION_MONTHS


def history_points(events: list[DebtEvent], tz: ZoneInfo) -> list[Point]:
    by_day: dict[date, int] = {}
    for e in events:  # já vêm em ordem cronológica: o último do dia sobrescreve
        by_day[e.occurred_at.astimezone(tz).date()] = e.balance_after_cents
    return sorted(by_day.items())


def _growth(d: Debt, today: date) -> list[Point]:
    points = [(today, d.balance_cents)]
    balance = d.balance_cents
    for k in range(1, GROWTH_MONTHS + 1):
        balance += finance.monthly_interest(balance, d.rate_ppm) - (d.payment_cents or 0)
        points.append((finance.payoff_date(today, k, d.due_day), balance))
    return points


def build(d: Debt, events: list[DebtEvent], today: date, tz: ZoneInfo) -> Evolution:
    history = history_points(events, tz)
    if d.kind in REVOLVING and d.balance_cents == 0:
        return Evolution(history, [], "em_dia", None, False)

    p = finance.project(d.balance_cents, d.rate_ppm, d.payment_cents)
    if p.status in ("crescendo", "estagnada"):
        return Evolution(history, _growth(d, today), p.status, None, False)
    if p.status in ("quitada", "sem_pagamento"):
        return Evolution(history, [], p.status, None, False)

    shown = p.schedule[:MAX_PROJECTION_MONTHS]
    projection = [(today, d.balance_cents)] + [
        (finance.payoff_date(today, m.number, d.due_day), m.balance_after_cents) for m in shown
    ]
    payoff = finance.payoff_date(today, p.months, d.due_day) if p.months else None
    return Evolution(history, projection, p.status, payoff, len(p.schedule) > len(shown))
