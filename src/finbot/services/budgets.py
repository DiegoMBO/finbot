"""Orçamentos: limite mensal de gastos por categoria.

Decisões:
- Categorias são comparadas SEM acento e sem maiúsculas: "/orcamento alimentacao 800"
  vale para os gastos em "alimentação". Ninguém precisa lembrar do acento.
- O alerta só dispara quando um gasto CRUZA 80% ou 100% do limite. Se avisasse
  a cada gasto acima de 80%, o aviso viraria ruído e você pararia de ler.
- O limite é o mesmo todo mês (não há histórico de limites). Um /mes de um mês
  passado compara com o limite atual.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from finbot.parsing.categories import EXPENSE_KEYWORDS
from finbot.parsing.text import normalize
from finbot.services import transactions
from finbot.services.transactions import Transaction

THRESHOLDS = (100, 80)  # do maior para o menor: se cruzar os dois de uma vez, vale o 100


@dataclass(frozen=True)
class Budget:
    category: str
    limit_cents: int


@dataclass(frozen=True)
class BudgetStatus:
    category: str
    limit_cents: int
    spent_cents: int

    @property
    def percent(self) -> int:
        # Divisão inteira: 79,9% aparece como 79%, nunca "arredonda para 80".
        return self.spent_cents * 100 // self.limit_cents

    @property
    def remaining_cents(self) -> int:
        return self.limit_cents - self.spent_cents  # negativo = estourou


@dataclass(frozen=True)
class BudgetAlert:
    threshold: int  # 80 ou 100
    status: BudgetStatus


def canonical_category(conn: sqlite3.Connection, raw: str) -> str:
    """Se já existe uma categoria "igual" (ignorando acento/maiúscula), usa o nome dela."""
    wanted = normalize(raw)
    known = list(EXPENSE_KEYWORDS)
    known += [r["category"] for r in conn.execute("SELECT category FROM budgets")]
    known += [
        r["category"]
        for r in conn.execute("SELECT DISTINCT category FROM transactions WHERE kind = 'expense'")
    ]
    for name in known:
        if normalize(name) == wanted:
            return name
    return raw.lower()


def list_budgets(conn: sqlite3.Connection) -> list[Budget]:
    rows = conn.execute("SELECT category, limit_cents FROM budgets ORDER BY category").fetchall()
    return [Budget(r["category"], r["limit_cents"]) for r in rows]


def find_budget(conn: sqlite3.Connection, category: str) -> Budget | None:
    wanted = normalize(category)
    return next((b for b in list_budgets(conn) if normalize(b.category) == wanted), None)


def set_budget(conn: sqlite3.Connection, category: str, limit_cents: int) -> Budget:
    """Cria ou substitui o limite da categoria."""
    name = canonical_category(conn, category)
    with conn:
        existing = find_budget(conn, name)
        if existing:
            conn.execute("DELETE FROM budgets WHERE category = ?", (existing.category,))
        conn.execute(
            "INSERT INTO budgets (category, limit_cents) VALUES (?, ?)", (name, limit_cents)
        )
    return Budget(name, limit_cents)


def remove_budget(conn: sqlite3.Connection, category: str) -> Budget | None:
    existing = find_budget(conn, category)
    if existing:
        with conn:
            conn.execute("DELETE FROM budgets WHERE category = ?", (existing.category,))
    return existing


def _spent(items: list[Transaction], category: str) -> int:
    wanted = normalize(category)
    return sum(
        t.amount_cents for t in items if t.kind == "expense" and normalize(t.category) == wanted
    )


def statuses(budgets: list[Budget], items: list[Transaction]) -> list[BudgetStatus]:
    """Quanto já foi gasto de cada orçamento, considerando as transações dadas."""
    result = [BudgetStatus(b.category, b.limit_cents, _spent(items, b.category)) for b in budgets]
    return sorted(result, key=lambda s: s.percent, reverse=True)  # os mais apertados primeiro


def crossed_threshold(before_cents: int, after_cents: int, limit_cents: int) -> int | None:
    """Qual limite (100 ou 80%) foi cruzado ao passar de `before` para `after`, se algum."""
    for threshold in THRESHOLDS:
        line = threshold * limit_cents  # compara em inteiros (x100) para não usar float
        if before_cents * 100 < line <= after_cents * 100:
            return threshold
    return None


def check_alert(conn: sqlite3.Connection, t: Transaction, tz: ZoneInfo) -> BudgetAlert | None:
    """Chamado logo após registrar `t`: o gasto fez a categoria cruzar 80% ou 100%?"""
    if t.kind != "expense":
        return None
    budget = find_budget(conn, t.category)
    if budget is None:
        return None
    local = t.created_at.astimezone(tz)
    items = transactions.list_between(conn, *transactions.month_bounds(local.year, local.month, tz))
    after = _spent(items, budget.category)
    threshold = crossed_threshold(after - t.amount_cents, after, budget.limit_cents)
    if threshold is None:
        return None
    return BudgetAlert(threshold, BudgetStatus(budget.category, budget.limit_cents, after))


def month_statuses(conn: sqlite3.Connection, now: datetime, tz: ZoneInfo) -> list[BudgetStatus]:
    local = now.astimezone(tz)
    items = transactions.list_between(conn, *transactions.month_bounds(local.year, local.month, tz))
    return statuses(list_budgets(conn), items)
