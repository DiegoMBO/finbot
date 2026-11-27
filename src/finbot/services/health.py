"""Indicadores de saúde financeira e sugestões baseadas nos SEUS dados.

Regras (todas transparentes: a resposta mostra os números usados):

Indicadores
- Base: os 3 últimos meses COMPLETOS que têm algum registro. Sem nenhum mês
  completo com dados, usa o mês atual até hoje (e avisa que é parcial).
- Renda = média das entradas nesses meses.
- Gastos fixos = média dos gastos nas categorias de FIXED_CATEGORIES.
- Parcelas = soma do pagamento mensal das dívidas ativas com saldo. O cartão de
  crédito fica de fora: a fatura é consumo, que já aparece nos gastos.
- Comprometimento = parcelas ÷ renda. Faixas: até HEALTH_OK_MAX% saudável,
  até HEALTH_WARN_MAX% atenção, acima disso alerta.
- Sobra = renda − gastos fixos − parcelas.

Sugestões
- Compara cada categoria no último mês completo com a média dos 3 meses
  anteriores a ele (só os meses com registros entram na média).
- Entra quem cresceu pelo menos SUGGEST_MIN_GROWTH_PCT% E pelo menos
  SUGGEST_MIN_GROWTH_BRL reais. A categoria "dívidas" (pagamentos) fica de fora.
- Para cada uma: se SUGGEST_SHARE_PCT% do aumento fosse para a dívida de juros
  mais altos, quantos meses e quantos reais de juros você economizaria.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo

from finbot.config import HealthSettings
from finbot.parsing.text import normalize
from finbot.services import credit_cards, debts, finance, salary, transactions
from finbot.services.debts import Debt
from finbot.services.finance import ExtraSimulation
from finbot.services.transactions import Transaction

Month = tuple[int, int]  # (ano, mês)
Band = Literal["saudável", "atenção", "alerta"]


def months_before(year: int, month: int, n: int) -> list[Month]:
    """Os n meses anteriores a (ano, mês), do mais antigo para o mais recente."""
    return [finance.add_months(year, month, -k) for k in range(n, 0, -1)]


def _items(conn: sqlite3.Connection, m: Month, tz: ZoneInfo) -> list[Transaction]:
    return transactions.list_between(conn, *transactions.month_bounds(m[0], m[1], tz))


def classify(commitment_bp: int, ok_pct: int, warn_pct: int) -> Band:
    """bp = pontos-base (1% = 100). O limite da faixa é inclusivo: 30,0% é saudável."""
    if commitment_bp <= ok_pct * 100:
        return "saudável"
    if commitment_bp <= warn_pct * 100:
        return "atenção"
    return "alerta"


@dataclass(frozen=True)
class Indicators:
    months: list[Month]  # meses usados como base
    partial: bool  # True = sem mês completo com dados; usou o mês atual até hoje
    income_cents: int
    fixed_cents: int
    installments_cents: int
    installment_debts: list[str]  # nomes das dívidas cujas parcelas entraram na conta
    total_debt_cents: int
    commitment_bp: int | None  # None quando não há renda registrada
    band: Band | None
    leftover_cents: int
    # Cartões: parcelas de compras de ANTES do bot na fatura atual. Elas não estão
    # nos gastos (o bot não viu essas compras), então entram no comprometimento.
    card_pre_bot_cents: int = 0
    card_used_cents: int = 0  # total usado dos limites (o que ainda vai ser cobrado)
    salary_cents: int | None = None  # líquido do salário cadastrado (None = sem cadastro)

    @property
    def committed_cents(self) -> int:
        return self.installments_cents + self.card_pre_bot_cents

    @property
    def total_with_cards_cents(self) -> int:
        return self.total_debt_cents + self.card_used_cents


def _is_fixed(category: str, fixed: tuple[str, ...]) -> bool:
    return normalize(category) in {normalize(c) for c in fixed}


def counts_as_installment(d: Debt) -> bool:
    return d.kind != "cartao" and d.balance_cents > 0 and bool(d.payment_cents)


def indicators(
    conn: sqlite3.Connection, today: date, tz: ZoneInfo, cfg: HealthSettings
) -> Indicators:
    by_month = {m: _items(conn, m, tz) for m in months_before(today.year, today.month, 3)}
    months = [m for m, items in by_month.items() if items]
    partial = not months
    if partial:
        current = (today.year, today.month)
        by_month = {current: _items(conn, current, tz)}
        months = [current]
    selected = [t for m in months for t in by_month[m]]
    n = len(months)

    # Renda base: com salário cadastrado, o líquido mensal dele (antes do consignado)
    # + a média das OUTRAS entradas (o salário lançado pelo bot não conta duas vezes).
    salary_net = salary.monthly_net_estimate(conn, today)
    if salary_net is not None:
        others = sum(
            t.amount_cents
            for t in selected
            if t.kind == "income" and t.category not in salary.PAYROLL_CATEGORIES
        )
        income = salary_net + others // n
    else:
        income = sum(t.amount_cents for t in selected if t.kind == "income") // n
    fixed = (
        sum(
            t.amount_cents
            for t in selected
            if t.kind == "expense" and _is_fixed(t.category, cfg.fixed_categories)
        )
        // n
    )
    active = debts.list_active(conn)
    paying = [d for d in active if counts_as_installment(d)]
    installments = sum(d.payment_cents for d in paying)
    cards = credit_cards.list_cards(conn)
    pre_bot = sum(credit_cards.pre_bot_cents(conn, c, today) for c in cards)
    committed = installments + pre_bot
    commitment = committed * 10_000 // income if income > 0 else None
    return Indicators(
        months=months,
        partial=partial,
        income_cents=income,
        fixed_cents=fixed,
        installments_cents=installments,
        installment_debts=[d.name for d in paying],
        total_debt_cents=sum(d.balance_cents for d in active),
        commitment_bp=commitment,
        band=classify(commitment, cfg.ok_max_pct, cfg.warn_max_pct)
        if commitment is not None
        else None,
        leftover_cents=income - fixed - committed,
        card_pre_bot_cents=pre_bot,
        salary_cents=salary_net,
        card_used_cents=sum(c.used_cents for c in cards),
    )


@dataclass(frozen=True)
class Suggestion:
    category: str
    last_cents: int  # gasto no último mês completo
    average_cents: int  # média dos meses anteriores
    growth_cents: int
    growth_pct: int | None  # None = a categoria não existia nos meses anteriores
    redirect_cents: int  # parte do aumento que iria para a dívida
    target: Debt | None
    simulation: ExtraSimulation | None


@dataclass(frozen=True)
class SuggestionReport:
    last_month: Month
    base_months: list[Month]  # meses (com dados) que formaram a média
    items: list[Suggestion] = field(default_factory=list)


def highest_rate_debt(conn: sqlite3.Connection) -> Debt | None:
    """A dívida que mais custa: maior taxa; empate, maior saldo."""
    candidates = [d for d in debts.list_active(conn) if d.balance_cents > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda d: (d.rate_ppm, d.balance_cents))


def _expenses_by_category(items: list[Transaction]) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for t in items:
        if t.kind == "expense" and t.category != debts.DEBT_CATEGORY:
            totals[t.category] += t.amount_cents
    return totals


def suggestions(
    conn: sqlite3.Connection, today: date, tz: ZoneInfo, cfg: HealthSettings, limit: int = 3
) -> SuggestionReport:
    last = finance.add_months(today.year, today.month, -1)
    base = [m for m in months_before(last[0], last[1], 3) if _items(conn, m, tz)]
    report = SuggestionReport(last_month=last, base_months=base)
    last_items = _items(conn, last, tz)
    if not base or not last_items:
        return report

    last_totals = _expenses_by_category(last_items)
    base_totals: dict[str, int] = defaultdict(int)
    for m in base:
        for category, cents in _expenses_by_category(_items(conn, m, tz)).items():
            base_totals[category] += cents

    target = highest_rate_debt(conn)
    found: list[Suggestion] = []
    for category, last_cents in last_totals.items():
        average = base_totals.get(category, 0) // len(base)
        growth = last_cents - average
        if growth < cfg.min_growth_cents:
            continue
        if average > 0 and growth * 100 < cfg.min_growth_pct * average:
            continue
        redirect = growth * cfg.share_pct // 100
        simulation = (
            finance.simulate_extra(
                target.balance_cents, target.rate_ppm, target.payment_cents, redirect
            )
            if target
            else None
        )
        found.append(
            Suggestion(
                category=category,
                last_cents=last_cents,
                average_cents=average,
                growth_cents=growth,
                growth_pct=growth * 100 // average if average else None,
                redirect_cents=redirect,
                target=target,
                simulation=simulation,
            )
        )
    found.sort(key=lambda s: s.growth_cents, reverse=True)
    report.items.extend(found[:limit])
    return report
