"""Textos compartilhados por mais de um módulo de handlers.

Ficam aqui (e não em handlers.py) para evitar import circular: handlers.py
importa payments.py, e os dois precisam do texto do alerta de orçamento.
"""

from __future__ import annotations

from finbot.money import format_brl
from finbot.services.budgets import BudgetAlert, BudgetStatus


def format_budget_line(s: BudgetStatus) -> str:
    """Ex.: • alimentação: R$ 680,00 de R$ 800,00 (85%) ⚠️"""
    icon = " 🚨" if s.percent >= 100 else " ⚠️" if s.percent >= 80 else ""
    line = f"• {s.category}: {format_brl(s.spent_cents)} de {format_brl(s.limit_cents)}"
    return f"{line} ({s.percent}%){icon}"


def format_alert(alert: BudgetAlert) -> str:
    s = alert.status
    if alert.threshold >= 100:
        over = format_brl(-s.remaining_cents)
        return (
            f"🚨 Orçamento de {s.category} estourado: {format_brl(s.spent_cents)} "
            f"de {format_brl(s.limit_cents)} (passou {over})."
        )
    return (
        f"⚠️ Você já usou {s.percent}% do orçamento de {s.category}: "
        f"restam {format_brl(s.remaining_cents)} este mês."
    )
