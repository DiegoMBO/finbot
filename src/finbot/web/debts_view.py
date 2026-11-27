"""Tela de Dívidas: cartões de cada dívida, simulador e detalhe com gráfico.

As contas são as do bot: finance.project (/divida), finance.run_strategy
(/estrategia) e finance.simulate_extra (/simular).
"""

from __future__ import annotations

import sqlite3
from datetime import date

from finbot.bot.cash import pct
from finbot.bot.strategy import revolving_inputs, strategy_inputs
from finbot.money import format_brl
from finbot.parsing.period import MONTH_NAMES
from finbot.services import debts, finance, salary
from finbot.services.debts import Debt

MAX_EXTRA_CENTS = 300_000  # o controle deslizante vai até R$ 3.000 por mês


def month_name(today: date, months_ahead: int) -> str:
    y, m = finance.add_months(today.year, today.month, months_ahead)
    return f"{MONTH_NAMES[m - 1][:3]}/{y}"


def _initial_balance(conn: sqlite3.Connection, d: Debt) -> int:
    """Saldo no cadastro (o 1º ponto do histórico): base da barra de progresso."""
    row = conn.execute(
        "SELECT balance_after_cents FROM debt_events WHERE debt_id = ? AND kind = 'cadastro'"
        " ORDER BY id LIMIT 1",
        (d.id,),
    ).fetchone()
    return row["balance_after_cents"] if row else d.balance_cents


def _status_text(d: Debt, p: finance.Projection, today: date) -> tuple[str, str]:
    """(texto, tom) da situação da dívida."""
    if p.status == "quitavel":
        end = finance.payoff_date(today, p.months, d.due_day)
        juros = f", {format_brl(p.total_interest_cents)} de juros" if p.total_interest_cents else ""
        return f"Acaba em {MONTH_NAMES[end.month - 1][:3]}/{end.year} ({p.months}x{juros})", "good"
    if p.status == "sem_pagamento":
        grows = finance.monthly_interest(d.balance_cents, d.rate_ppm)
        if grows:
            return f"Sem parcela: cresce {format_brl(grows)} por mês", "critical"
        return "Sem parcela definida", "neutral"
    if p.status == "crescendo":
        return (f"A parcela não cobre os juros: cresce {format_brl(p.growth_per_month_cents)}"
                " por mês", "critical")  # fmt: skip
    if p.status == "estagnada":
        return "A parcela só paga os juros: o saldo não diminui", "warning"
    return "Leva mais de 50 anos para acabar com esta parcela", "warning"


def debts_page(conn: sqlite3.Connection, today: date) -> dict:
    active = [d for d in debts.list_active(conn) if d.kind != "cartao"]
    consigned = {x.debt_id for x in salary.deductions(conn) if x.debt_id}
    cards = []
    for d in active:
        p = debts.projection(d)
        text, tone = _status_text(d, p, today)
        initial = _initial_balance(conn, d)
        paid_pct = (
            max(0, min(100, round(100 * (initial - d.balance_cents) / initial))) if initial else 0
        )
        cards.append(
            {
                "id": d.id,
                "name": d.name,
                "kind": d.kind_label,
                "balance": d.balance_cents,
                "initial": initial,
                "paid_pct": paid_pct,
                "rate": pct(d.rate_ppm) if d.rate_ppm else "sem juros",
                "annual": pct(finance.annual_equivalent_ppm(d.rate_ppm)) if d.rate_ppm else "",
                "payment": d.payment_cents,
                "due_day": d.due_day,
                "remaining": d.remaining_installments,
                "consigned": d.id in consigned,
                "status": text,
                "tone": tone,
                "grows": not d.payment_cents and d.rate_ppm > 0,
            }
        )
    # Prioridade: a que cresce sozinha e a de maior taxa vêm primeiro (avalanche).
    cards.sort(key=lambda c: (not c["grows"], -debts.get(conn, c["id"]).rate_ppm, c["balance"]))
    inputs = strategy_inputs(active) + revolving_inputs(conn, today)
    return {
        "cards": cards,
        "total": sum(c["balance"] for c in cards),
        "monthly": sum(c["payment"] or 0 for c in cards),
        "has_inputs": bool(inputs),
        "max_extra": MAX_EXTRA_CENTS,
        "today": today,
        "options": [{"id": c["id"], "name": c["name"]} for c in cards],
    }


def _result_json(r: finance.StrategyResult, today: date) -> dict:
    return {
        "feasible": r.feasible,
        "months": r.total_months,
        "end": month_name(today, r.total_months) if r.total_months else None,
        "interest": r.total_interest_cents,
        "budget": r.monthly_budget_cents,
        "order": [
            {"name": n, "month": r.payoff_month[n], "when": month_name(today, r.payoff_month[n])}
            for n in r.payoff_order
        ],
        "trajectory": list(r.trajectory),
    }


def simulate(conn: sqlite3.Connection, today: date, extra_cents: int) -> dict:
    """Todas as dívidas com +extra por mês: avalanche, bola de neve e "só as parcelas"."""
    extra_cents = max(0, min(extra_cents, MAX_EXTRA_CENTS))
    active = [d for d in debts.list_active(conn) if d.kind != "cartao"]
    inputs = strategy_inputs(active) + revolving_inputs(conn, today)
    if not inputs:
        return {"ready": False}
    base = finance.run_strategy(inputs, "avalanche", 0)
    avalanche, snowball = finance.compare_strategies(inputs, extra_cents)
    start = sum(d.balance_cents for d in inputs)
    horizon = max(len(base.trajectory), len(avalanche.trajectory), 1)
    labels = [month_name(today, 0)] + [month_name(today, k) for k in range(1, horizon + 1)]
    out = {
        "ready": True,
        "extra": extra_cents,
        "start": start,
        "labels": labels,
        "base": _result_json(base, today),
        "avalanche": _result_json(avalanche, today),
        "snowball": _result_json(snowball, today),
    }
    if base.feasible and avalanche.feasible:
        out["saved_interest"] = base.total_interest_cents - avalanche.total_interest_cents
        out["saved_months"] = base.total_months - avalanche.total_months
    return out


def debt_detail(conn: sqlite3.Connection, debt_id: int, today: date, tz) -> dict | None:
    try:
        d = debts.get(conn, debt_id)
    except TypeError:  # id que não existe
        return None
    if d is None:
        return None
    history = [
        {
            "date": e.occurred_at.astimezone(tz).date().isoformat(),
            "cents": e.balance_after_cents,
            "kind": e.kind,
            "amount": e.amount_cents,
            "interest": e.interest_cents,
        }
        for e in debts.events(conn, d.id)
    ]
    p = debts.projection(d)
    projection = []
    if p.schedule:
        for m in p.schedule:
            due = finance.payoff_date(today, m.number, d.due_day)
            projection.append({"date": due.isoformat(), "cents": m.balance_after_cents})
    elif d.rate_ppm and not d.payment_cents:
        # Sem parcela: mostra como cresce em 12 meses sem pagar nada
        balance = d.balance_cents
        for k in range(1, 13):
            balance += finance.monthly_interest(balance, d.rate_ppm)
            y, m = finance.add_months(today.year, today.month, k)
            projection.append({"date": date(y, m, 1).isoformat(), "cents": balance})
    text, tone = _status_text(d, p, today)
    return {
        "debt": d,
        "rate": pct(d.rate_ppm) if d.rate_ppm else "sem juros",
        "annual": pct(finance.annual_equivalent_ppm(d.rate_ppm)) if d.rate_ppm else "",
        "status": text,
        "tone": tone,
        "history": history,
        "projection": projection,
        "events": list(reversed(debts.events(conn, d.id)))[:20],
        "max_extra": MAX_EXTRA_CENTS,
        "today": today,
    }


def simulate_one(conn: sqlite3.Connection, debt_id: int, today: date, extra_cents: int) -> dict:
    """/simular de uma dívida: pagar o de sempre × o de sempre + extra."""
    d = debts.get(conn, debt_id)
    extra_cents = max(0, min(extra_cents, MAX_EXTRA_CENTS))
    s = finance.simulate_extra(d.balance_cents, d.rate_ppm, d.payment_cents, extra_cents)

    def side(p: finance.Projection) -> dict:
        end = finance.payoff_date(today, p.months, d.due_day) if p.months else None
        return {
            "status": p.status,
            "months": p.months,
            "end": f"{MONTH_NAMES[end.month - 1][:3]}/{end.year}" if end else None,
            "interest": p.total_interest_cents,
            "path": [m.balance_after_cents for m in p.schedule],
        }

    return {
        "extra": extra_cents,
        "start": d.balance_cents,
        "base": side(s.base),
        "with_extra": side(s.with_extra),
        "months_saved": s.months_saved,
        "interest_saved": s.interest_saved_cents,
    }
