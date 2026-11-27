"""Cálculos financeiros das dívidas. Sem banco e sem Telegram: só números.

Convenções (as mesmas do resto do projeto):
- dinheiro em CENTAVOS (int);
- taxa de juros mensal em MILIONÉSIMOS (int, "ppm"): 2% a.m. = 0,02 = 20000 ppm.
  Assim toda a conta de juros do mês é feita com inteiros, sem erro de float.

Modelo: a cada mês, o saldo recebe os juros do mês e depois o pagamento abate:
    juros  = saldo × i                     (arredondado para o centavo, "meio para cima")
    saldo' = saldo + juros − pagamento
É exatamente o sistema Price quando o pagamento é a parcela fixa do contrato.
Calculamos MÊS A MÊS (e não só pela fórmula fechada) porque assim tratamos a
última parcela menor, pagamentos extras e as estratégias com várias dívidas.
As fórmulas fechadas (price_payment, months_to_payoff_formula, present_value)
servem para calcular a parcela e para conferir a simulação nos testes.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Literal

PPM = 1_000_000  # 100% = 1.000.000 ppm
MAX_MONTHS = 1200  # 100 anos: além disso consideramos "não quita"

Status = Literal["quitada", "quitavel", "estagnada", "crescendo", "sem_pagamento", "longa"]
Strategy = Literal["avalanche", "bola_de_neve"]


# --- conversões de taxa -----------------------------------------------------


def _rate(rate_ppm: int) -> Decimal:
    return Decimal(rate_ppm) / PPM


def _to_ppm(value: Decimal) -> int:
    return int((value * PPM).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def annual_equivalent_ppm(monthly_ppm: int) -> int:
    """Taxa anual equivalente: (1 + i)^12 − 1. Ex.: 15% a.m. ≈ 435% a.a."""
    with localcontext() as ctx:
        ctx.prec = 30
        return _to_ppm((1 + _rate(monthly_ppm)) ** 12 - 1)


def annual_to_monthly_ppm(annual_ppm: int) -> int:
    """Taxa mensal equivalente: (1 + i_anual)^(1/12) − 1. Ex.: 12% a.a. ≈ 0,9489% a.m."""
    with localcontext() as ctx:
        ctx.prec = 30
        return _to_ppm((1 + _rate(annual_ppm)) ** (Decimal(1) / 12) - 1)


# --- um mês de juros --------------------------------------------------------


def monthly_interest(balance_cents: int, rate_ppm: int) -> int:
    """Juros de um mês em centavos, arredondando meio centavo para cima.

    Conta só com inteiros: (saldo × ppm + 500.000) // 1.000.000.
    Ex.: R$ 179,49 a 10% = 17,949 -> R$ 17,95.
    """
    if balance_cents < 0 or rate_ppm < 0:
        raise ValueError("saldo e taxa não podem ser negativos")
    return (balance_cents * rate_ppm + PPM // 2) // PPM


# --- fórmulas fechadas do sistema Price -------------------------------------


def price_payment(principal_cents: int, rate_ppm: int, months: int) -> int:
    """Parcela fixa (Price): A = P × i / (1 − (1 + i)^−n). Com i = 0: A = P / n."""
    if months <= 0:
        raise ValueError("o número de parcelas precisa ser positivo")
    if rate_ppm == 0:
        return -(-principal_cents // months)  # divisão arredondada para cima
    with localcontext() as ctx:
        ctx.prec = 30
        i = _rate(rate_ppm)
        payment = Decimal(principal_cents) * i / (1 - (1 + i) ** -months)
        return int(payment.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def present_value(payment_cents: int, rate_ppm: int, months: int) -> int:
    """Quanto valem hoje n parcelas: PV = A × (1 − (1 + i)^−n) / i."""
    if rate_ppm == 0:
        return payment_cents * months
    with localcontext() as ctx:
        ctx.prec = 30
        i = _rate(rate_ppm)
        pv = Decimal(payment_cents) * (1 - (1 + i) ** -months) / i
        return int(pv.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def months_to_payoff_formula(
    principal_cents: int, rate_ppm: int, payment_cents: int
) -> Decimal | None:
    """n = −ln(1 − P·i/A) / ln(1 + i), sem arredondar. None se a parcela não cobre os juros."""
    if payment_cents <= 0:
        return None
    if rate_ppm == 0:
        return Decimal(principal_cents) / payment_cents
    with localcontext() as ctx:
        ctx.prec = 30
        i = _rate(rate_ppm)
        ratio = Decimal(principal_cents) * i / payment_cents
        if ratio >= 1:
            return None
        return -((1 - ratio).ln()) / (1 + i).ln()


# --- projeção de uma dívida -------------------------------------------------


@dataclass(frozen=True)
class Month:
    number: int  # 1 = próximo pagamento
    interest_cents: int
    payment_cents: int
    balance_after_cents: int


@dataclass(frozen=True)
class Projection:
    status: Status
    months: int | None  # meses até quitar (None se não quita)
    total_interest_cents: int  # juros que ainda serão pagos até quitar
    total_paid_cents: int
    last_payment_cents: int
    # Quanto a dívida aumenta por mês quando a parcela não cobre os juros.
    growth_per_month_cents: int = 0
    schedule: list[Month] = field(default_factory=list, repr=False)


def project(
    balance_cents: int,
    rate_ppm: int,
    payment_cents: int | None,
    extra_cents: int = 0,
    max_months: int = MAX_MONTHS,
) -> Projection:
    """Projeta a dívida mantendo o pagamento mensal (mais um extra opcional)."""
    if balance_cents <= 0:
        return Projection("quitada", 0, 0, 0, 0)
    pay = (payment_cents or 0) + extra_cents
    if pay <= 0:
        return Projection("sem_pagamento", None, 0, 0, 0)

    first_interest = monthly_interest(balance_cents, rate_ppm)
    if pay < first_interest:
        return Projection("crescendo", None, 0, 0, 0, growth_per_month_cents=first_interest - pay)
    if pay == first_interest:
        return Projection("estagnada", None, 0, 0, 0)

    balance, total_interest, total_paid, last = balance_cents, 0, 0, 0
    schedule: list[Month] = []
    while balance > 0 and len(schedule) < max_months:
        interest = monthly_interest(balance, rate_ppm)
        due = balance + interest
        last = min(pay, due)  # a última parcela é só o que falta
        balance = due - last
        total_interest += interest
        total_paid += last
        schedule.append(Month(len(schedule) + 1, interest, last, balance))

    if balance > 0:
        return Projection("longa", None, total_interest, total_paid, last, schedule=schedule)
    return Projection(
        "quitavel", len(schedule), total_interest, total_paid, last, schedule=schedule
    )


@dataclass(frozen=True)
class ExtraSimulation:
    base: Projection
    with_extra: Projection
    months_saved: int | None
    interest_saved_cents: int | None


def simulate_extra(
    balance_cents: int, rate_ppm: int, payment_cents: int | None, extra_cents: int
) -> ExtraSimulation:
    """/simular: compara pagar o de sempre com pagar o de sempre + extra todo mês."""
    base = project(balance_cents, rate_ppm, payment_cents)
    with_extra = project(balance_cents, rate_ppm, payment_cents, extra_cents)
    if base.months is None or with_extra.months is None:
        # Sem prazo de base (dívida crescendo, por exemplo) não há "economia" a medir.
        return ExtraSimulation(base, with_extra, None, None)
    return ExtraSimulation(
        base,
        with_extra,
        base.months - with_extra.months,
        base.total_interest_cents - with_extra.total_interest_cents,
    )


# --- estratégias com várias dívidas -----------------------------------------


@dataclass(frozen=True)
class DebtInput:
    name: str
    balance_cents: int
    rate_ppm: int
    payment_cents: int  # pagamento mínimo/parcela atual (0 se não houver)
    # Pagamento obrigatório que MUDA a cada mês (faturas de cartão): mês 1, 2, 3...
    # Quando informado, substitui payment_cents; depois do último mês, é 0.
    schedule: tuple[int, ...] = ()

    def minimum(self, month: int) -> int:
        if self.schedule:
            return self.schedule[month - 1] if month <= len(self.schedule) else 0
        return self.payment_cents


@dataclass(frozen=True)
class StrategyResult:
    strategy: Strategy
    feasible: bool  # False: o orçamento não quita tudo dentro de MAX_MONTHS
    payoff_order: list[str]  # nomes na ordem em que ficam quitadas
    payoff_month: dict[str, int]  # nome -> mês em que foi quitada
    total_months: int | None
    total_interest_cents: int
    monthly_budget_cents: int


def _priority(strategy: Strategy, balances: dict[str, int], debts: dict[str, DebtInput]):
    """Chave de ordenação da dívida-alvo (a primeira recebe o dinheiro que sobra)."""
    if strategy == "avalanche":
        # Maior juros primeiro; empate: menor saldo.
        return lambda name: (-debts[name].rate_ppm, balances[name], name)
    # Bola de neve: menor saldo (atual) primeiro; empate: maior juros.
    return lambda name: (balances[name], -debts[name].rate_ppm, name)


def run_strategy(
    debts: list[DebtInput],
    strategy: Strategy,
    extra_cents: int = 0,
    max_months: int = MAX_MONTHS,
) -> StrategyResult:
    """Simula a quitação de todas as dívidas com um orçamento mensal FIXO.

    Orçamento = soma das parcelas atuais + extra. A cada mês:
    1. todas as dívidas recebem os juros do mês;
    2. cada uma recebe sua parcela (ou o que falta, se for menos);
    3. a sobra vai para a dívida-alvo da estratégia; se ela quitar, o resto
       vai para a próxima.
    Como o orçamento não muda, a parcela de uma dívida quitada passa a
    reforçar as outras (o "efeito bola de neve" vale para as duas estratégias).
    """
    by_name = {d.name: d for d in debts}
    balances = {d.name: d.balance_cents for d in debts if d.balance_cents > 0}
    # Orçamento fixo = o que sai no 1º mês (parcelas + faturas do mês) + extra.
    budget = sum(d.minimum(1) for d in debts if d.balance_cents > 0) + extra_cents
    payoff_month: dict[str, int] = {}
    # Ordem REAL em que as dívidas zeram (dentro do mês também): quem a estratégia
    # quitou primeiro aparece primeiro, mesmo que outra termine no mesmo mês.
    order: list[str] = []
    total_interest = 0
    month = 0

    def pay(name: str, amount: int) -> None:
        balances[name] -= amount
        if balances[name] == 0 and name not in payoff_month:
            payoff_month[name] = month
            order.append(name)

    while balances and month < max_months and budget > 0:
        month += 1
        for name in balances:
            interest = monthly_interest(balances[name], by_name[name].rate_ppm)
            balances[name] += interest
            total_interest += interest

        available = budget
        for name in balances:
            paid = min(by_name[name].minimum(month), balances[name], available)
            pay(name, paid)
            available -= paid

        for name in sorted(balances, key=_priority(strategy, balances, by_name)):
            if available <= 0:
                break
            paid = min(available, balances[name])
            pay(name, paid)
            available -= paid

        for name in [n for n, b in balances.items() if b == 0]:
            del balances[name]

    feasible = not balances
    return StrategyResult(
        strategy=strategy,
        feasible=feasible,
        payoff_order=order,
        payoff_month=payoff_month,
        total_months=month if feasible else None,
        total_interest_cents=total_interest,
        monthly_budget_cents=budget,
    )


def compare_strategies(
    debts: list[DebtInput], extra_cents: int = 0
) -> tuple[StrategyResult, StrategyResult]:
    return run_strategy(debts, "avalanche", extra_cents), run_strategy(
        debts, "bola_de_neve", extra_cents
    )


# --- datas ------------------------------------------------------------------


def due_date(year: int, month: int, day: int) -> date:
    """Vencimento no mês; dia 31 em mês de 30 dias vira o último dia do mês."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def add_months(year: int, month: int, k: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + k
    return total // 12, total % 12 + 1


def next_due_date(today: date, due_day: int) -> date:
    """Próximo vencimento a partir de hoje (hoje conta, se for o dia)."""
    this_month = due_date(today.year, today.month, due_day)
    if this_month >= today:
        return this_month
    year, month = add_months(today.year, today.month, 1)
    return due_date(year, month, due_day)


def payoff_date(today: date, months: int, due_day: int | None) -> date:
    """Data do último pagamento.

    Com dia de vencimento: o próximo vencimento conta como o mês 1.
    Sem dia de vencimento: o mês 1 é daqui a um mês, no mesmo dia de hoje.
    """
    if months <= 0:
        return today
    if due_day is None:
        year, month = add_months(today.year, today.month, months)
        return due_date(year, month, today.day)
    first = next_due_date(today, due_day)
    year, month = add_months(first.year, first.month, months - 1)
    return due_date(year, month, due_day)
