"""Cálculos da folha de pagamento CLT. Sem banco e sem Telegram: só números e datas.

Regras (referências):
- INSS progressivo por faixas, até o teto (tabelas em payroll_tables.py).
- IRRF: base = rendimento − deduções; usa o MELHOR para você entre as deduções
  legais (INSS + dependentes) e o desconto simplificado. Depois aplica a redução
  da Lei 15.270/2025 (calculada sobre o rendimento tributável bruto).
- Hora extra (CF art. 7º XVI; CLT art. 59): valor-hora = salário ÷ (jornada
  semanal × 5); adicional mínimo de 50%; domingo/feriado normalmente 100%.
- DSR sobre horas extras habituais (Lei 605/49; Súmula 172 TST):
  (horas extras do mês ÷ dias úteis) × (domingos + feriados).
- 13º (Lei 4.090/62 e 4.749/65): 1/12 por mês com 15 dias ou mais trabalhados;
  1ª parcela = metade, sem descontos; 2ª = o resto, com INSS e IR exclusivos.
- Férias (CLT arts. 129 a 145): 30 dias a cada 12 meses; + 1/3 (CF art. 7º XVII);
  venda de até 10 dias (abono pecuniário, art. 143), sem INSS e sem IR;
  devem ser dadas nos 12 meses seguintes, senão são pagas em dobro (art. 137).
- Salário do mês é pago até o 5º dia útil do mês seguinte (art. 459); para isso,
  sábado conta como dia útil.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from finbot.services.finance import add_months
from finbot.services.payroll_tables import tables_for

Month = tuple[int, int]  # (ano, mês)


def _cents(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


cents = _cents  # público: arredonda centavos (meio para cima)


# --- INSS e IRRF -------------------------------------------------------------


def inss(gross_cents: int, year: int) -> int:
    """Contribuição progressiva: cada alíquota só sobre a parte do salário na faixa."""
    total = Decimal(0)
    lower = 0
    for bracket in tables_for(year).inss:
        if gross_cents <= lower:
            break
        part = min(gross_cents, bracket.up_to_cents) - lower
        total += Decimal(part) * bracket.rate_bp / 10_000
        lower = bracket.up_to_cents
    return _cents(total)


@dataclass(frozen=True)
class IncomeTax:
    base_cents: int
    used_simplified: bool  # True = desconto simplificado foi melhor
    table_tax_cents: int  # pela tabela, antes da redução
    reduction_cents: int  # Lei 15.270/2025
    tax_cents: int  # o que é descontado


def irrf(taxable_cents: int, inss_cents: int, dependents: int, year: int) -> IncomeTax:
    t = tables_for(year)
    legal = inss_cents + dependents * t.dependent_deduction_cents
    used_simplified = t.simplified_deduction_cents > legal
    base = max(taxable_cents - max(legal, t.simplified_deduction_cents), 0)

    table_tax = 0
    for bracket in t.irrf:
        if bracket.up_to_cents is None or base <= bracket.up_to_cents:
            value = Decimal(base) * bracket.rate_bp / 10_000 - bracket.deduction_cents
            table_tax = max(_cents(value), 0)
            break

    if taxable_cents <= t.reduction_full_up_to_cents:
        reduction = table_tax
    elif taxable_cents <= t.reduction_until_cents:
        value = Decimal(t.reduction_constant_cents) - (
            Decimal(t.reduction_factor_millionths) * taxable_cents / 1_000_000
        )
        reduction = min(max(_cents(value), 0), table_tax)
    else:
        reduction = 0
    return IncomeTax(base, used_simplified, table_tax, reduction, table_tax - reduction)


# --- calendário: feriados, dias úteis, dia do pagamento -----------------------


def easter(year: int) -> date:
    """Domingo de Páscoa (algoritmo anônimo gregoriano)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month = (h + m - 7 * n + 114) // 31
    day = (h + m - 7 * n + 114) % 31 + 1
    return date(year, month, day)


FIXED_HOLIDAYS = ((1, 1), (4, 21), (5, 1), (9, 7), (10, 12), (11, 2), (11, 15), (11, 20), (12, 25))


def holidays(year: int, extra: tuple[tuple[int, int], ...] = ()) -> set[date]:
    """Feriados nacionais (incluindo a Sexta-feira Santa) + os extras (dia, mês)."""
    days = {date(year, m, d) for m, d in FIXED_HOLIDAYS}
    days.add(easter(year) - timedelta(days=2))
    days |= {date(year, m, d) for d, m in extra}
    return days


def rest_and_business_days(year: int, month: int, extra=()) -> tuple[int, int]:
    """(domingos + feriados, dias úteis) do mês, para o DSR."""
    hol = holidays(year, extra)
    days_in_month = calendar.monthrange(year, month)[1]
    rest = sum(
        1
        for d in range(1, days_in_month + 1)
        if (day := date(year, month, d)).weekday() == 6 or day in hol
    )
    return rest, days_in_month - rest


def is_business_day(day: date, extra=()) -> bool:
    """Dia útil para pagamento de salário: segunda a sábado, exceto feriado."""
    return day.weekday() != 6 and day not in holidays(day.year, extra)


def payday(year: int, month: int, rule: str, extra=()) -> date:
    """Dia em que o salário cai no mês `month` ('5du' ou 'dia:N')."""
    if rule.startswith("dia:"):
        last = calendar.monthrange(year, month)[1]
        day = date(year, month, min(int(rule[4:]), last))
        while not is_business_day(day, extra):  # cai em domingo/feriado: antecipa
            day -= timedelta(days=1)
        return day
    count, day = 0, date(year, month, 1)
    while True:
        if is_business_day(day, extra):
            count += 1
            if count == 5:
                return day
        day += timedelta(days=1)


# --- horas extras -------------------------------------------------------------


def hourly_rate(gross_cents: int, weekly_hours: int) -> Decimal:
    """Valor da hora em centavos (sem arredondar): salário ÷ (jornada × 5)."""
    return Decimal(gross_cents) / (weekly_hours * 5)


@dataclass(frozen=True)
class OvertimeMonth:
    month: Month  # mês em que as horas foram feitas (competência)
    minutes_by_pct: dict[int, int]
    base_cents: int  # horas × valor-hora × (1 + adicional)
    dsr_cents: int  # reflexo no descanso semanal
    rest_days: int
    business_days: int

    @property
    def total_cents(self) -> int:
        return self.base_cents + self.dsr_cents

    @property
    def minutes(self) -> int:
        return sum(self.minutes_by_pct.values())


def overtime_month(
    month: Month, minutes_by_pct: dict[int, int], hourly: Decimal, extra=()
) -> OvertimeMonth:
    base = _cents(
        sum(
            (
                Decimal(minutes) / 60 * hourly * (100 + pct) / 100
                for pct, minutes in minutes_by_pct.items()
            ),
            Decimal(0),
        )
    )
    rest, business = rest_and_business_days(month[0], month[1], extra)
    dsr = _cents(Decimal(base) / business * rest) if business else 0
    return OvertimeMonth(month, dict(minutes_by_pct), base, dsr, rest, business)


def overtime_competence(worked_on: date, closing_day: int | None) -> Month:
    """Em que "mês de ponto" a hora extra entra: depois do fechamento, vai para o seguinte."""
    if closing_day is None or worked_on.day <= closing_day:
        return worked_on.year, worked_on.month
    return add_months(worked_on.year, worked_on.month, 1)


def overtime_arrival(worked_on: date, closing_day: int | None, lag_months: int) -> Month:
    """Mês em que o dinheiro das horas extras CAI na conta."""
    y, m = overtime_competence(worked_on, closing_day)
    return add_months(y, m, lag_months)


# --- 13º salário ----------------------------------------------------------------


def thirteenth_months(year: int, admission: date, up_to_month: int = 12) -> int:
    """Quantos 1/12 do 13º no ano: meses com 15 dias ou mais trabalhados."""
    count = 0
    for month in range(1, up_to_month + 1):
        if (year, month) < (admission.year, admission.month):
            continue
        if (year, month) == (admission.year, admission.month):
            days_in_month = calendar.monthrange(year, month)[1]
            if days_in_month - admission.day + 1 < 15:
                continue
        count += 1
    return count


@dataclass(frozen=True)
class ThirteenthPayment:
    pay_date: date
    kind: str  # "13-primeira" (sem descontos) ou "13-segunda"
    gross_cents: int  # o que esta data paga, antes dos descontos
    inss_cents: int
    irrf_cents: int
    net_cents: int


def thirteenth_plan(
    year: int,
    admission: date,
    base_cents: int,  # salário + média dos variáveis
    schedule: list[tuple[int, int]],  # [(dia, mês)]; a última data é a 2ª parcela
    dependents: int = 0,
    first_paid_override: dict[int, int] | None = None,  # {mês: valor real recebido}
    first_base_cents: int | None = None,  # base da 1ª parcela (padrão: base_cents)
) -> list[ThirteenthPayment]:
    """Os pagamentos do 13º no ano, conforme as datas da empresa.

    Cada data antes da última completa a 1ª parcela: metade do 13º proporcional
    até aquele mês, menos o que já foi pago. A última data paga o resto, com
    INSS e IR calculados sobre o 13º inteiro (tributação exclusiva).
    """
    *firsts, (last_day, last_month) = sorted(schedule, key=lambda dm: (dm[1], dm[0]))
    payments: list[ThirteenthPayment] = []
    paid_first = 0
    first_base = base_cents if first_base_cents is None else first_base_cents
    for day, month in firsts:
        target = _cents(Decimal(first_base) * thirteenth_months(year, admission, month) / 24)
        amount = (first_paid_override or {}).get(month, max(target - paid_first, 0))
        if amount > 0:
            payments.append(
                ThirteenthPayment(date(year, month, day), "13-primeira", amount, 0, 0, amount)
            )
            paid_first += amount
    total = _cents(Decimal(base_cents) * thirteenth_months(year, admission, 12) / 12)
    if total == 0:
        return payments
    contribution = inss(total, year)
    tax = irrf(total, contribution, dependents, year).tax_cents
    remaining = total - paid_first
    payments.append(
        ThirteenthPayment(
            date(year, last_month, last_day),
            "13-segunda",
            remaining,
            contribution,
            tax,
            remaining - contribution - tax,
        )
    )
    return payments


# --- férias ---------------------------------------------------------------------


@dataclass(frozen=True)
class VacationPay:
    days: int
    sold_days: int
    vacation_cents: int  # dias de férias
    third_cents: int  # 1/3 constitucional
    bonus_cents: int  # abono pecuniário (dias vendidos + 1/3): sem INSS e sem IR
    inss_cents: int
    irrf_cents: int

    @property
    def gross_cents(self) -> int:
        return self.vacation_cents + self.third_cents + self.bonus_cents

    @property
    def net_cents(self) -> int:
        return self.gross_cents - self.inss_cents - self.irrf_cents


def vacation_pay(
    base_cents: int, days: int, sold_days: int, dependents: int, year: int
) -> VacationPay:
    """Valor das férias (estimativa: INSS e IR calculados só sobre as férias)."""
    if not 5 <= days <= 30 or not 0 <= sold_days <= 10 or days + sold_days > 30:
        raise ValueError("férias: 5 a 30 dias, vendendo no máximo 10, total até 30")
    vacation = _cents(Decimal(base_cents) * days / 30)
    third = _cents(Decimal(vacation) / 3)
    bonus = _cents(Decimal(base_cents) * sold_days / 30 * 4 / 3)
    taxable = vacation + third
    contribution = inss(taxable, year)
    tax = irrf(taxable, contribution, dependents, year).tax_cents
    return VacationPay(days, sold_days, vacation, third, bonus, contribution, tax)


@dataclass(frozen=True)
class VacationPeriod:
    acquisition_start: date
    acquisition_end: date  # a partir do dia seguinte, pode tirar
    concession_deadline: date  # até aqui, a empresa tem de dar (senão paga em dobro)


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29/02
        return d.replace(year=d.year + years, day=28)


def vacation_periods(admission: date, until: date) -> list[VacationPeriod]:
    """Períodos aquisitivos que já começaram até `until`."""
    periods = []
    start = admission
    while start <= until:
        end = _add_years(start, 1) - timedelta(days=1)
        deadline = _add_years(end, 1)
        periods.append(VacationPeriod(start, end, deadline))
        start = end + timedelta(days=1)
    return periods


def vacation_days_in_month(start: date, days: int, year: int, month: int) -> int:
    """Quantos dias de férias (começando em `start`) caem no mês."""
    end = start + timedelta(days=days - 1)
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    overlap = (min(end, last) - max(start, first)).days + 1
    return max(overlap, 0)
