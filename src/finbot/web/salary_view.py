"""Tela de Salário: holerite, próximos pagamentos (gráfico por mês), horas extras,
13º e férias. As contas são as do bot (services/salary.py e payroll.py)."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from finbot.bot import salary as salary_bot
from finbot.parsing.period import MONTH_NAMES
from finbot.parsing.salary_text import format_minutes
from finbot.services import payroll, salary
from finbot.services.finance import add_months

KIND_ORDER = ("vale", "salario", "13-primeira", "13-segunda", "ferias")
KIND_GROUP = {  # agrupa as duas parcelas do 13º numa cor só no gráfico
    "vale": "Adiantamento",
    "salario": "Salário",
    "13-primeira": "13º",
    "13-segunda": "13º",
    "ferias": "Férias",
}
GROUP_SLOT = {"Salário": 1, "Adiantamento": 2, "13º": 3, "Férias": 4}


def _short(y: int, m: int) -> str:
    return f"{MONTH_NAMES[m - 1][:3]}/{y}"


def payslip_rows(s: salary.Payslip) -> list[tuple[str, int, str]]:
    """Linhas do holerite: (rótulo, valor, tipo) — tipo: base | plus | minus | total."""
    label = "Salário" + (f" ({s.worked_days} de 30 dias)" if s.worked_days < 30 else "")
    rows = [(label, s.salary_cents, "base")]
    for ot in s.overtime:
        rows.append(
            (f"Horas extras de {_short(*ot.month)} ({format_minutes(ot.minutes)}) + DSR",
             ot.total_cents, "plus")
        )  # fmt: skip
    rows.append(("Bruto", s.gross_cents, "total"))
    rows.append(("INSS", s.inss_cents, "minus"))
    ir = "IR" + (" (zerado pela Lei 15.270)" if s.ir.reduction_cents and not s.ir.tax_cents else "")
    rows.append((ir, s.ir.tax_cents, "minus"))
    for name, value in s.deductions:
        rows.append((name, value, "minus"))
    if s.advance_cents:
        rows.append(("Adiantamento já pago", s.advance_cents, "minus"))
    rows.append(("Líquido", s.net_cents, "total"))
    for debt, value in s.consigned:
        rows.append((f"Consignado {debt.name} (paga a dívida)", value, "minus"))
    if s.consigned:
        rows.append(("Cai na conta", s.received_cents, "total"))
    return rows


def salary_page(conn: sqlite3.Connection, today: date, extra=()) -> dict:
    employment = salary.get_employment(conn)
    profile = salary.profile_at(conn, today)
    if employment is None or profile is None:
        return {"ready": False}
    current = (today.year, today.month)
    slip = salary.payslip(conn, add_months(*current, -1), extra)
    if slip is None or slip.pay_date < today:
        slip = salary.payslip(conn, current, extra)
    upcoming = salary.payments_between(conn, today, today + timedelta(days=92), extra)

    # Gráfico: o que cai na conta em cada um dos próximos 6 meses, por tipo
    months = [add_months(*current, k) for k in range(6)]
    end = date(*add_months(*months[-1], 1), 1) - timedelta(days=1)
    by_month: dict[tuple[int, int], dict[str, int]] = {m: {} for m in months}
    for u in salary.payments_between(conn, today, end, extra):
        key = (u.pay_date.year, u.pay_date.month)
        group = KIND_GROUP[u.kind]
        by_month[key][group] = by_month[key].get(group, 0) + u.received_cents
    groups = [g for g in GROUP_SLOT if any(g in v for v in by_month.values())]
    chart = {
        "months": [_short(*m) for m in months],
        "series": [
            {"name": g, "slot": GROUP_SLOT[g], "values": [by_month[m].get(g, 0) for m in months]}
            for g in groups
        ],
    }

    overtime = []
    for comp, ot in salary.overtime_by_competence(conn, profile, extra).items():
        arrival = add_months(*comp, profile.overtime_lag_months)
        pay = payroll.payday(*arrival, profile.payday_rule, extra)
        overtime.append(
            {"month": _short(*comp), "hours": format_minutes(ot.minutes), "base": ot.base_cents,
             "dsr": ot.dsr_cents, "total": ot.total_cents, "pay": pay, "received": pay < today}
        )  # fmt: skip
    hourly = profile.hourly
    year = today.year if today <= date(today.year, 12, 20) else today.year + 1
    return {
        "ready": True,
        "profile": profile,
        "employment": employment,
        "hourly": payroll.cents(hourly),
        "hourly_ot": payroll.cents(hourly * (100 + profile.overtime_pct) / 100),
        "hourly_holiday": payroll.cents(hourly * (100 + profile.holiday_pct) / 100),
        "payday": salary_bot.payday_text(profile.payday_rule),
        "schedule": salary.format_schedule(employment.schedule),
        "slip": slip,
        "slip_rows": payslip_rows(slip) if slip else [],
        "slip_label": _short(*slip.competence) if slip else "",
        "upcoming": upcoming,
        "chart": chart,
        "overtime": overtime,
        "to_receive": sum(o["total"] for o in overtime if not o["received"]),
        "net_month": salary.monthly_net_estimate(conn, today),
        "thirteenth_text": salary_bot.format_thirteenth(conn, year, today, extra),
        "vacations_text": salary_bot.format_vacations(conn, today, extra),
        "today": today,
    }
