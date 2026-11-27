"""Cálculos do cartão de crédito. Sem banco e sem Telegram: só datas e números.

Ciclo da fatura (dia de fechamento C, dia de vencimento V):
- Compra ANTES do fechamento do mês entra na fatura que fecha neste mês;
  compra NO DIA do fechamento ou depois entra na fatura seguinte
  (por isso o fechamento é o "melhor dia de compra").
- O vencimento da fatura é o primeiro dia V depois do fechamento.
- Dia que não existe no mês (31 em abril, 30 em fevereiro) vira o último dia.

Exemplos (C = 3, V = 10):   compra 02/12 -> fecha 03/12, vence 10/12
                            compra 03/12 -> fecha 03/01, vence 10/01
         (C = 28, V = 5):   compra 27/11 -> fecha 28/11, vence 05/12
                            compra 28/11 -> fecha 28/12, vence 05/01
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from finbot.services.finance import PPM, add_months, due_date, monthly_interest, next_due_date

USAGE_THRESHOLDS = (100, 90, 80)  # % do limite; do maior para o menor


@dataclass(frozen=True)
class Cycle:
    closing: date
    due: date

    @property
    def reference_month(self) -> str:
        return f"{self.due:%Y-%m}"


def _next_month(d: date) -> tuple[int, int]:
    return add_months(d.year, d.month, 1)


def due_after(closing: date, due_day: int) -> date:
    """Primeiro vencimento DEPOIS do fechamento."""
    candidate = due_date(closing.year, closing.month, due_day)
    if candidate > closing:
        return candidate
    year, month = _next_month(closing)
    return due_date(year, month, due_day)


def closing_before(due: date, closing_day: int) -> date:
    """O fechamento da fatura que vence em `due` (o último fechamento antes dele)."""
    candidate = due_date(due.year, due.month, closing_day)
    if candidate < due:
        return candidate
    year, month = add_months(due.year, due.month, -1)
    return due_date(year, month, closing_day)


def cycle_for_purchase(purchase: date, closing_day: int, due_day: int) -> Cycle:
    """Em qual fatura cai uma compra feita em `purchase`."""
    closing = due_date(purchase.year, purchase.month, closing_day)
    if purchase >= closing:  # no dia do fechamento já vai para a próxima
        year, month = _next_month(purchase)
        closing = due_date(year, month, closing_day)
    return Cycle(closing, due_after(closing, due_day))


def cycle_for_due(due: date, closing_day: int) -> Cycle:
    return Cycle(closing_before(due, closing_day), due)


def shift(cycle: Cycle, k: int, closing_day: int, due_day: int) -> Cycle:
    """A fatura k meses depois (parcela k+1 de uma compra parcelada)."""
    year, month = add_months(cycle.due.year, cycle.due.month, k)
    return cycle_for_due(due_date(year, month, due_day), closing_day)


def payable_cycle(today: date, closing_day: int, due_day: int) -> Cycle:
    """A fatura "atual": a próxima a vencer (hoje conta). Pode já estar fechada."""
    return cycle_for_due(next_due_date(today, due_day), closing_day)


# --- parcelas ---------------------------------------------------------------


def split_installments(total_cents: int, n: int) -> list[int]:
    """Divide em n parcelas de centavos inteiros; a sobra vai para a 1ª.

    R$ 100,00 em 3x -> 33,34 + 33,33 + 33,33.
    """
    if n < 1:
        raise ValueError("o número de parcelas precisa ser pelo menos 1")
    if total_cents < n:
        raise ValueError("valor pequeno demais para tantas parcelas")
    base = total_cents // n
    return [base + (total_cents - base * n)] + [base] * (n - 1)


def implicit_rate_ppm(cash_price_cents: int, installment_cents: int, n: int) -> int:
    """Taxa mensal embutida num parcelamento (sistema Price), por bisseção.

    Acha i tal que  parcela × (1 − (1 + i)^−n) / i = preço à vista.
    Sem juros (parcelas somam o preço à vista ou menos) -> 0.
    Ex.: R$ 3.000 à vista em 10x de R$ 330 -> ~1,77% a.m.
    """
    if installment_cents * n <= cash_price_cents:
        return 0
    with localcontext() as ctx:
        ctx.prec = 30
        target = Decimal(cash_price_cents)
        payment = Decimal(installment_cents)

        def present_value(i: Decimal) -> Decimal:
            return payment * (1 - (1 + i) ** -n) / i

        low, high = Decimal("1e-9"), Decimal(1)  # entre ~0% e 100% ao mês
        for _ in range(100):
            mid = (low + high) / 2
            if present_value(mid) > target:  # PV cai quando a taxa sobe
                low = mid
            else:
                high = mid
        return int((low * PPM).to_integral_value())


# --- rotativo e limite ------------------------------------------------------


def revolving(remaining_cents: int, rate_ppm: int) -> tuple[int, int]:
    """O que falta pagar, levado à fatura seguinte: (juros, total com juros).

    Ex.: faltaram R$ 500 a 15% -> juros R$ 75 -> R$ 575 na próxima fatura.
    """
    interest = monthly_interest(remaining_cents, rate_ppm)
    return interest, remaining_cents + interest


def usage_bp(used_cents: int, limit_cents: int) -> int:
    """Uso do limite em pontos-base (1% = 100)."""
    return used_cents * 10_000 // limit_cents


def crossed_usage(before_cents: int, after_cents: int, limit_cents: int) -> int | None:
    """Qual faixa de uso (100, 90 ou 80%) foi cruzada, se alguma."""
    for threshold in USAGE_THRESHOLDS:
        line = threshold * limit_cents
        if before_cents * 100 < line <= after_cents * 100:
            return threshold
    return None


def best_card(cards: list[tuple[str, int, int]], today: date) -> tuple[str, date] | None:
    """Com qual cartão uma compra feita hoje vence mais tarde.

    `cards` = [(nome, dia_fechamento, dia_vencimento)]. Empate: o primeiro da lista.
    """
    best: tuple[str, date] | None = None
    for name, closing_day, due_day in cards:
        due = cycle_for_purchase(today, closing_day, due_day).due
        if best is None or due > best[1]:
            best = (name, due)
    return best
