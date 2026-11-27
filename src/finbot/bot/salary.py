"""Salário no Telegram: /salario (cadastro e resumo), /editarsalario, horas extras
("fiz 10 horas extras"), /horasextras, /decimo, /ferias e o lançamento automático
do que cai na conta (salário, vale, 13º e férias)."""

from __future__ import annotations

import sqlite3
import warnings
from datetime import UTC, date, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from finbot.bot import flows
from finbot.config import Settings
from finbot.money import format_brl
from finbot.parsing import debt as pd
from finbot.parsing import salary_text as st
from finbot.parsing.debt import FieldError, is_skip
from finbot.parsing.text import normalize
from finbot.services import debts, payroll, salary
from finbot.services.finance import add_months
from finbot.services.salary import Payslip, Upcoming, month_label

DRAFT = "salary_draft"
EDITING = "salary_edit"
CB = "salario:"
ASKING, CONFIRM = range(30, 32)
MENU, EDIT_ASK = range(32, 34)


def _db(context) -> sqlite3.Connection:
    return context.bot_data["db"]


def _settings(context) -> Settings:
    return context.bot_data["settings"]


def _today(context) -> date:
    return datetime.now(_settings(context).timezone).date()


def _extra(context) -> tuple:
    return _settings(context).extra_holidays


def _brl(cents: int) -> str:
    return format_brl(cents)


# --- textos -------------------------------------------------------------------------


def payday_text(rule: str) -> str:
    return "5º dia útil" if rule == "5du" else f"dia {rule[4:]}"


def format_payslip(s: Payslip) -> list[str]:
    lines = [f"🧾 Salário de {month_label(s.competence)}, cai em {s.pay_date:%d/%m/%Y}"]
    salary_line = f"   Salário {_brl(s.salary_cents)}"
    if s.worked_days < 30:
        salary_line += f" ({s.worked_days} de 30 dias)"
    lines.append(salary_line)
    for ot in s.overtime:
        lines.append(
            f"   + Horas extras de {month_label(ot.month)} "
            f"({st.format_minutes(ot.minutes)}): {_brl(ot.base_cents)} + DSR {_brl(ot.dsr_cents)}"
        )
    lines.append(f"   = Bruto {_brl(s.gross_cents)}")
    lines.append(f"   − INSS {_brl(s.inss_cents)}")
    ir = s.ir
    ir_line = f"   − IR {_brl(ir.tax_cents)}"
    if ir.reduction_cents:
        ir_line += (
            f" (pela tabela {_brl(ir.table_tax_cents)}; "
            f"redução da Lei 15.270 −{_brl(ir.reduction_cents)})"
        )
    lines.append(ir_line)
    for name, value in s.deductions:
        lines.append(f"   − {name} {_brl(value)}")
    if s.advance_cents:
        lines.append(f"   − vale já pago {_brl(s.advance_cents)}")
    lines.append(f"   = Líquido {_brl(s.net_cents)}")
    for debt, value in s.consigned:
        lines.append(f"   − Consignado {debt.name} {_brl(value)} (vira pagamento da dívida)")
    if s.consigned:
        lines.append(f"   = Cai na conta {_brl(s.received_cents)}")
    return lines


def format_upcoming(items: list[Upcoming]) -> list[str]:
    return [f"   {u.pay_date:%d/%m} {u.label}: {_brl(u.received_cents)}" for u in items]


def format_summary(conn: sqlite3.Connection, today: date, extra=()) -> str:
    employment = salary.get_employment(conn)
    profile = salary.profile_at(conn, today)
    hourly = profile.hourly
    ot = payroll.cents(hourly * (100 + profile.overtime_pct) / 100)
    lines = [
        "💼 Salário",
        f"Bruto {_brl(profile.gross_cents)} · jornada {profile.weekly_hours}h "
        f"(hora {_brl(payroll.cents(hourly))}; extra {profile.overtime_pct}% {_brl(ot)})",
        f"Admissão {employment.admission_date:%d/%m/%Y} · cai no {payday_text(profile.payday_rule)}"
        f" · horas extras caem {profile.overtime_lag_months} mês(es) depois",
        f"13º pago em: {salary.format_schedule(employment.schedule)}",
    ]
    if profile.advance_pct:
        lines.append(f"Vale: {profile.advance_pct}% no dia {profile.advance_day}")
    current = (today.year, today.month)
    slip = salary.payslip(conn, add_months(*current, -1), extra)
    if slip is None or slip.pay_date < today:
        slip = salary.payslip(conn, current, extra)
    if slip is not None:
        lines += [""] + format_payslip(slip)
    upcoming = salary.payments_between(conn, today, today + timedelta(days=60), extra)
    if upcoming:
        lines += ["", "📅 Próximos 60 dias (o que cai na conta):"] + format_upcoming(upcoming)
    net = salary.monthly_net_estimate(conn, today)
    lines.append(f"\nRenda base (/saude): {_brl(net)} de líquido por mês, sem horas extras")
    lines.append("Editar: /editarsalario · /horasextras · /decimo · /ferias")
    return "\n".join(lines)


def format_post(r: salary.PostResult) -> str:
    u = r.upcoming
    lines = [f"💰 Caiu: {u.label} ({u.pay_date:%d/%m/%Y})"]
    if u.kind == "salario":
        lines.append(
            f"Bruto {_brl(u.gross_cents)} · INSS −{_brl(u.inss_cents)} · IR −{_brl(u.irrf_cents)}"
            + (f" · descontos −{_brl(u.deductions_cents)}" if u.deductions_cents else "")
        )
    elif u.kind == "13-segunda":
        lines.append(f"INSS −{_brl(u.inss_cents)} · IR −{_brl(u.irrf_cents)}")
    elif u.kind == "ferias":
        lines.append(
            f"Bruto {_brl(u.gross_cents)} · INSS −{_brl(u.inss_cents)} · IR −{_brl(u.irrf_cents)}"
        )
    lines.append(f"Entrada registrada: {_brl(u.net_cents)}")
    for p in r.debt_payments:
        lines.append(
            f"Consignado {p.debt.name}: −{_brl(p.amount_cents)} "
            f"(pagamento registrado; saldo {_brl(p.balance_after_cents)})"
        )
    if r.debt_payments:
        lines.append(f"Na conta: {_brl(u.received_cents)}")
    if u.kind == "13-primeira":
        lines.append("Caiu outro valor? /decimo recebi VALOR")
    lines.append("Não caiu ou veio diferente? /desfazer")
    return "\n".join(lines)


# --- lançamento automático (chamado pela tarefa diária) ------------------------------


async def run_daily(context) -> int:
    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    today = datetime.now(settings.timezone).date()
    sent = 0
    for up in salary.pending_posts(conn, today, settings.extra_holidays):
        result = salary.post(conn, up, datetime.now(UTC), settings.timezone)
        await context.bot.send_message(settings.allowed_chat_id, format_post(result))
        sent += 1
    text = vacation_deadline_warning(conn, today)
    if text:
        await context.bot.send_message(settings.allowed_chat_id, text)
        sent += 1
    return sent


def vacation_deadline_warning(conn: sqlite3.Connection, today: date) -> str | None:
    """Aviso 60 e 30 dias antes do prazo para a empresa dar as férias (senão, em dobro)."""
    employment = salary.get_employment(conn)
    if employment is None:
        return None
    taken = salary.vacations(conn)
    for period in payroll.vacation_periods(employment.admission_date, today):
        days_left = (period.concession_deadline - today).days
        if days_left not in (60, 30) or period.acquisition_end >= today:
            continue
        used = sum(
            v.days + v.sold_days for v in taken
            if period.acquisition_end < v.start_date <= period.concession_deadline
        )  # fmt: skip
        if used < 30:
            return (
                f"🏖️ Atenção: as férias do período {period.acquisition_start:%d/%m/%Y} a "
                f"{period.acquisition_end:%d/%m/%Y} têm de começar até "
                f"{period.concession_deadline:%d/%m/%Y} ({days_left} dias). Depois disso, a "
                "empresa paga em dobro (CLT art. 137). Já marcou? /ferias"
            )
    return None


# --- horas extras -------------------------------------------------------------------


async def try_overtime(message: Message, context, text: str) -> bool:
    """ "fiz 10 horas extras": registra e responde com o valor. True se tratou."""
    intent = st.parse_overtime(text, _today(context))
    if intent is None:
        return False
    conn = _db(context)
    profile = salary.profile_at(conn, intent.worked_on)
    if profile is None:
        await message.reply_text(
            "Para calcular as horas extras, cadastre o salário primeiro: /salario"
        )
        return True
    pct = profile.holiday_pct if intent.holiday else profile.overtime_pct
    salary.add_overtime(conn, intent.worked_on, intent.minutes, pct, datetime.now(UTC))
    await message.reply_text(overtime_reply(conn, profile, intent, pct, _extra(context)))
    return True


def overtime_reply(conn, profile, intent: st.OvertimeIntent, pct: int, extra=()) -> str:
    comp = payroll.overtime_competence(intent.worked_on, profile.closing_day)
    month = salary.overtime_by_competence(conn, profile, extra)[comp]
    one = payroll.cents(profile.hourly * intent.minutes / 60 * (100 + pct) / 100)
    arrival = add_months(*comp, profile.overtime_lag_months)
    pay = payroll.payday(*arrival, profile.payday_rule, extra)
    return (
        f"⏱️ Registrado: {st.format_minutes(intent.minutes)} extras ({pct}%) em "
        f"{intent.worked_on:%d/%m/%Y} = {_brl(one)} brutos\n"
        f"Total de {month_label(comp)}: {st.format_minutes(month.minutes)} → "
        f"{_brl(month.base_cents)} + DSR {_brl(month.dsr_cents)} = {_brl(month.total_cents)}\n"
        f"Cai no salário pago em {pay:%d/%m/%Y} (bruto; o INSS/IR sai junto com o salário)\n"
        "Errou? /desfazer"
    )


def format_overtime(conn, profile, today: date, extra=()) -> str:
    hourly = profile.hourly
    lines = [
        "⏱️ Horas extras",
        f"Hora normal {_brl(payroll.cents(hourly))} · {profile.overtime_pct}% = "
        f"{_brl(payroll.cents(hourly * (100 + profile.overtime_pct) / 100))} · "
        f"domingo/feriado {profile.holiday_pct}% = "
        f"{_brl(payroll.cents(hourly * (100 + profile.holiday_pct) / 100))}",
    ]
    by_comp = salary.overtime_by_competence(conn, profile, extra)
    if not by_comp:
        lines.append("Nada registrado. Ex.: fiz 2h30 extras · fiz 4 horas extras no domingo")
        return "\n".join(lines)
    to_receive = 0
    for comp, ot in by_comp.items():
        arrival = add_months(*comp, profile.overtime_lag_months)
        pay = payroll.payday(*arrival, profile.payday_rule, extra)
        status = "a receber" if pay >= today else "já pago"
        if pay >= today:
            to_receive += ot.total_cents
        lines.append(
            f"{month_label(comp)}: {st.format_minutes(ot.minutes)} → {_brl(ot.base_cents)} + DSR "
            f"{_brl(ot.dsr_cents)} = {_brl(ot.total_cents)} · {status} em {pay:%d/%m/%Y}"
        )
    lines.append(f"A receber (bruto): {_brl(to_receive)}")
    lines.append(
        f"(O DSR é o reflexo no descanso: horas extras ÷ dias úteis × domingos e feriados. "
        f"Caem {profile.overtime_lag_months} mês(es) depois; ajuste em /editarsalario.)"
    )
    return "\n".join(lines)


async def overtime_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = _db(context)
    profile = salary.profile_at(conn, _today(context))
    if profile is None:
        await update.effective_message.reply_text("Cadastre o salário primeiro: /salario")
        return
    await update.effective_message.reply_text(
        format_overtime(conn, profile, _today(context), _extra(context))
    )


# --- 13º e férias -------------------------------------------------------------------


def format_thirteenth(conn, year: int, today: date, extra=()) -> str:
    employment = salary.get_employment(conn)
    profile = salary.profile_at(conn, date(year, 12, 1))
    plan = salary.thirteenth_payments(conn, year, extra)
    months = payroll.thirteenth_months(year, employment.admission_date)
    lines = [f"🎄 13º salário de {year}"]
    lines.append(
        f"Meses que contam: {months}/12 (admissão {employment.admission_date:%d/%m/%Y}; "
        "mês com 15 dias ou mais trabalhados conta)"
    )
    if not plan:
        lines.append("Nada a receber neste ano.")
        return "\n".join(lines)
    last = plan[-1]
    total = sum(u.gross_cents for u in plan)
    avg = total * 12 // months - profile.gross_cents if months else 0
    base = f"Base: salário {_brl(profile.gross_cents)}"
    if avg > 0:
        base += f" + média de horas extras {_brl(avg)}"
    lines.append(base)
    lines.append(
        f"13º bruto {_brl(total)} · INSS −{_brl(last.inss_cents)} · IR −{_brl(last.irrf_cents)}"
        f" → líquido {_brl(total - last.inss_cents - last.irrf_cents)}"
    )
    posted = {
        (r["kind"], r["reference"]): r["status"]
        for r in conn.execute("SELECT * FROM payroll_payments")
    }
    for u in plan:
        status = posted.get((u.kind, u.reference))
        mark = " ✅ lançado" if status == "lancado" else (" (desfeito)" if status else "")
        what = "sem descontos" if u.kind == "13-primeira" else "com INSS e IR do 13º inteiro"
        lines.append(
            f"{u.pay_date:%d/%m} {salary.KIND_LABEL[u.kind]}: {_brl(u.net_cents)} ({what}){mark}"
        )
    lines.append("Caiu outro valor na 1ª parcela? /decimo recebi VALOR (a 2ª se ajusta)")
    return "\n".join(lines)


async def thirteenth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    conn = _db(context)
    today = _today(context)
    if salary.get_employment(conn) is None:
        await message.reply_text("Cadastre o salário primeiro: /salario")
        return
    args = list(context.args or [])
    if args and normalize(args[0]) in {"recebi", "caiu", "valor"}:
        try:
            amount = pd.parse_money(" ".join(args[1:]))
        except FieldError as exc:
            await message.reply_text(f"Não entendi: {exc}. Ex.: /decimo recebi 1125")
            return
        firsts = [
            u for u in salary.thirteenth_payments(conn, today.year, _extra(context))
            if u.kind == "13-primeira"
        ]  # fmt: skip
        target = max((u for u in firsts if u.pay_date <= today + timedelta(days=5)),
                     key=lambda u: u.pay_date, default=firsts[0] if firsts else None)  # fmt: skip
        if target is None:
            await message.reply_text("Não há 1ª parcela do 13º neste ano.")
            return
        salary.correct_first_thirteenth(
            conn, target.pay_date.year, target.pay_date.month, amount, datetime.now(UTC)
        )
        await message.reply_text(
            f"✅ 1ª parcela de {target.pay_date:%d/%m} ajustada para {_brl(amount)}.\n\n"
            + format_thirteenth(conn, today.year, today, _extra(context))
        )
        return
    year = today.year if today <= date(today.year, 12, 20) else today.year + 1
    await message.reply_text(format_thirteenth(conn, year, today, _extra(context)))


def format_vacations(conn, today: date, extra=()) -> str:
    employment = salary.get_employment(conn)
    profile = salary.profile_at(conn, today)
    lines = ["🏖️ Férias"]
    for i, per in enumerate(payroll.vacation_periods(employment.admission_date, today), 1):
        lines.append(
            f"{i}º período: {per.acquisition_start:%d/%m/%Y} a {per.acquisition_end:%d/%m/%Y} → "
            f"pode tirar a partir de {per.acquisition_end + timedelta(days=1):%d/%m/%Y}; "
            f"a empresa tem até {per.concession_deadline:%d/%m/%Y} (depois, paga em dobro)"
        )
    year = today.year + (1 if today.month >= 7 else 0)
    full = payroll.vacation_pay(profile.gross_cents, 30, 0, profile.dependents, year)
    sold = payroll.vacation_pay(profile.gross_cents, 20, 10, profile.dependents, year)
    lines += [
        "",
        f"Simulação (salário {_brl(profile.gross_cents)}; "
        "a média de horas extras entra quando marcar):",
        f"• 30 dias: {_brl(full.net_cents)} líquidos (férias {_brl(full.vacation_cents)} + 1/3 "
        f"{_brl(full.third_cents)} − INSS {_brl(full.inss_cents)} − IR {_brl(full.irrf_cents)})",
        f"• 20 dias + vender 10: {_brl(sold.net_cents)} líquidos (abono {_brl(sold.bonus_cents)} "
        "sem INSS e IR; você trabalha esses 10 dias)",
        "⚠️ No mês seguinte às férias o salário vem menor: os dias de férias já vieram adiantados.",
    ]
    marked = salary.vacations(conn)
    if marked:
        lines += ["", "Marcadas:"]
        for v in marked:
            up = salary.vacation_payment(conn, v, extra)
            text = (
                f"• {v.start_date:%d/%m/%Y} a {v.end_date:%d/%m/%Y} ({v.days} dias"
                + (f" + {v.sold_days} vendidos" if v.sold_days else "")
                + f"): recebe {_brl(up.net_cents)} em {v.pay_date:%d/%m/%Y}"
            )
            lines.append(text)
            for comp in sorted(
                {(v.start_date.year, v.start_date.month), (v.end_date.year, v.end_date.month)}
            ):
                slip = salary.payslip(conn, comp, extra)
                if slip and slip.worked_days < 30:
                    lines.append(
                        f"   salário de {month_label(comp)} (cai {slip.pay_date:%d/%m}): "
                        f"{slip.worked_days} dias → {_brl(slip.received_cents)}"
                    )
    lines += [
        "",
        "Marcar: /ferias marcar 15/07/2027 30 (ou 20 vender 10) · Apagar: /ferias apagar",
        "Dica: pedindo em janeiro, a 1ª parcela do 13º pode vir junto com as férias.",
    ]
    return "\n".join(lines)


async def vacation_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    conn = _db(context)
    today = _today(context)
    if salary.get_employment(conn) is None:
        await message.reply_text("Cadastre o salário primeiro: /salario")
        return
    args = [normalize(a) for a in (context.args or [])]
    if args and args[0] == "apagar":
        marked = salary.vacations(conn)
        if not marked:
            await message.reply_text("Não há férias marcadas.")
            return
        salary.delete_vacation(conn, marked[-1].id)
        await message.reply_text(f"🗑️ Férias de {marked[-1].start_date:%d/%m/%Y} apagadas.")
        return
    if args and args[0] == "marcar":
        try:
            start = pd.parse_date(args[1], date(today.year + 3, 12, 31))
            days = int(args[2])
            sold = int(args[4]) if len(args) >= 5 and args[3] == "vender" else 0
            salary.add_vacation(conn, start, days, sold, datetime.now(UTC))
        except (IndexError, ValueError, FieldError) as exc:
            await message.reply_text(
                f"Não entendi ({exc}). Ex.: /ferias marcar 15/07/2027 30 · "
                "/ferias marcar 15/07/2027 20 vender 10"
            )
            return
        employment = salary.get_employment(conn)
        first_ok = payroll.vacation_periods(employment.admission_date, start)[0].acquisition_end
        warn = (
            f"\n⚠️ Antes de {first_ok + timedelta(days=1):%d/%m/%Y} "
            "você ainda não completou 12 meses."
            if start <= first_ok
            else ""
        )
        await message.reply_text(
            "✅ Férias marcadas." + warn + "\n\n" + format_vacations(conn, today, _extra(context))
        )
        return
    await message.reply_text(format_vacations(conn, today, _extra(context)))


# --- cadastro guiado ------------------------------------------------------------------

QUESTIONS = {
    "gross_cents": ("Qual o seu salário BRUTO mensal? Ex.: 4.500", False),
    "admission": ("Data de admissão? Ex.: 06/07/2026", False),
    "weekly_hours": ("Jornada semanal, em horas? Ex.: 40 ou 44", False),
    "overtime_pct": ("Adicional de hora extra, em %? Pular = 50%", True),
    "dependents": ("Quantos dependentes no Imposto de Renda? Pular = 0", True),
    "payday_rule": ("Quando o salário cai? Ex.: 5º dia útil, ou dia 10. Pular = 5º dia útil", True),
    "advance": (
        "A empresa paga vale (adiantamento) no meio do mês? Ex.: 40% dia 20. Pular = não",
        True,
    ),
    "schedule": (
        "Em que datas a empresa paga o 13º? Ex.: 30/06, 30/11, 18/12 (a última é a 2ª "
        "parcela, com descontos). Pular = 30/11 e 20/12 (prazos da lei)",
        True,
    ),
    "closing_day": ("Dia de fechamento do ponto (horas extras)? Pular = último dia do mês", True),
    "overtime_lag_months": (
        "As horas extras caem quantos meses depois do mês em que você fez? "
        "1 = no salário seguinte; 2 = dois meses depois. Pular = 2",
        True,
    ),
    "consigned": (
        "Tem empréstimo consignado descontado em folha? Escreva o nome da dívida. Pular = não",
        True,
    ),
    "deductions": (
        "Outros descontos fixos no holerite? Ex.: vale-transporte 6%, plano de saúde 150. "
        "Pular = nenhum",
        True,
    ),
}
ORDER = list(QUESTIONS)
DEFAULTS = {
    "overtime_pct": 50, "dependents": 0, "payday_rule": "5du", "advance": None,
    "schedule": [(30, 11), (20, 12)], "closing_day": None, "overtime_lag_months": 2,
    "consigned": None, "deductions": [],
}  # fmt: skip


def parse_answer(conn, field: str, text: str, today: date):
    if field == "gross_cents":
        return pd.parse_money(text)
    if field == "admission":
        return pd.parse_date(text, today)
    if field == "weekly_hours":
        return st.parse_int(text, 1, 44, "jornada")
    if field == "overtime_pct":
        return st.parse_int(text, 50, 300, "adicional")
    if field == "dependents":
        return st.parse_int(text, 0, 20, "dependentes")
    if field == "payday_rule":
        return st.parse_payday(text)
    if field == "advance":
        return st.parse_advance(text)
    if field == "schedule":
        try:
            return salary.parse_schedule(text)
        except ValueError as exc:
            raise FieldError(str(exc)) from exc
    if field == "closing_day":
        return st.parse_int(text, 1, 31, "dia")
    if field == "overtime_lag_months":
        return st.parse_int(text, 0, 3, "meses")
    if field == "consigned":
        found = debts.find(conn, text)
        if len(found) != 1:
            names = ", ".join(d.name for d in debts.list_active(conn)) or "nenhuma"
            raise FieldError(f"não achei uma dívida só com esse nome (suas dívidas: {names})")
        return found[0].id
    if field == "deductions":
        return st.parse_deductions(text)
    raise ValueError(field)


SKIP = InlineKeyboardMarkup([[InlineKeyboardButton("Pular ⏭", callback_data=f"{CB}skip")]])
CONFIRM_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("✅ Salvar", callback_data=f"{CB}save"),
      InlineKeyboardButton("❌ Cancelar", callback_data=f"{CB}cancel")]]
)  # fmt: skip


async def _ask(message: Message, draft: dict) -> int:
    question, optional = QUESTIONS[draft["_field"]]
    await message.reply_text(
        question + ("\n(opcional: Pular)" if optional else ""),
        reply_markup=SKIP if optional else None,
    )
    return ASKING


def save_draft(conn, draft: dict, now: datetime) -> None:
    values = {k: draft.get(k, DEFAULTS.get(k)) for k in ORDER}
    salary.save_employment(conn, values["admission"], values["schedule"], now)
    advance = values["advance"] or (None, None)
    salary.add_profile(
        conn, values["admission"], now, gross_cents=values["gross_cents"],
        weekly_hours=values["weekly_hours"], overtime_pct=values["overtime_pct"],
        dependents=values["dependents"], payday_rule=values["payday_rule"],
        advance_pct=advance[0], advance_day=advance[1], closing_day=values["closing_day"],
        overtime_lag_months=values["overtime_lag_months"],
    )  # fmt: skip
    if values["consigned"]:
        debt = debts.get(conn, values["consigned"])
        salary.add_deduction(conn, f"consignado {debt.name}", now, debt_id=debt.id)
    for spec in values["deductions"]:
        salary.add_deduction(
            conn, spec.name, now, percent_bp=spec.percent_bp, fixed_cents=spec.fixed_cents
        )


async def _advance(message: Message, context, value) -> int:
    draft = context.user_data[DRAFT]
    field = draft["_field"]
    draft[field] = DEFAULTS.get(field) if value is None else value
    idx = ORDER.index(field)
    if idx + 1 < len(ORDER):
        draft["_field"] = ORDER[idx + 1]
        return await _ask(message, draft)
    gross, hours = draft["gross_cents"], draft["weekly_hours"]
    today = _today(context)
    inss = payroll.inss(gross, today.year)
    ir = payroll.irrf(gross, inss, draft.get("dependents") or 0, today.year)
    months = payroll.thirteenth_months(today.year, draft["admission"])
    lines = [
        "Confira:",
        f"Bruto {_brl(gross)} · {hours}h semanais "
        f"(hora {_brl(payroll.cents(payroll.hourly_rate(gross, hours)))})",
        f"Admissão {draft['admission']:%d/%m/%Y} · "
        f"cai no {payday_text(draft.get('payday_rule') or '5du')}",
        f"13º em {salary.format_schedule(draft.get('schedule') or DEFAULTS['schedule'])} · "
        f"{months}/12 em {today.year}",
        f"Horas extras: {draft.get('overtime_pct') or 50}%, "
        f"caem {draft.get('overtime_lag_months', 2)} mês(es) depois",
        "",
        f"Líquido estimado: {_brl(gross)} − INSS {_brl(inss)} − IR {_brl(ir.tax_cents)} = "
        f"{_brl(gross - inss - ir.tax_cents)} (antes de outros descontos e consignado)",
        "Confira com o seu holerite: se bater, as tabelas estão certas.",
    ]
    await message.reply_text("\n".join(lines), reply_markup=CONFIRM_KB)
    return CONFIRM


async def salary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/salario: resumo; se ainda não há cadastro, começa o cadastro."""
    message = update.effective_message
    conn = _db(context)
    if salary.get_employment(conn) and salary.profiles(conn):
        await message.reply_text(format_summary(conn, _today(context), _extra(context)))
        return ConversationHandler.END
    if flows.busy(context.user_data, DRAFT):
        await message.reply_text(flows.BUSY_TEXT)
        return ConversationHandler.END
    context.user_data[DRAFT] = {"_field": ORDER[0]}
    await message.reply_text(
        "Vamos cadastrar o seu salário (CLT). /cancelar sai a qualquer momento."
    )
    return await _ask(message, context.user_data[DRAFT])


async def on_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    draft = context.user_data.get(DRAFT)
    if draft is None:
        return ConversationHandler.END
    field = draft["_field"]
    text = message.text or ""
    if QUESTIONS[field][1] and is_skip(text):
        return await _advance(message, context, None)
    try:
        value = parse_answer(_db(context), field, text, _today(context))
    except FieldError as exc:
        await message.reply_text(f"Não entendi: {exc}.")
        return await _ask(message, draft)
    return await _advance(message, context, value)


async def on_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    draft = context.user_data.get(DRAFT)
    if draft is None or not QUESTIONS[draft["_field"]][1]:
        await query.answer("Esse botão é de uma pergunta anterior.")
        return ASKING if draft else ConversationHandler.END
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    return await _advance(query.message, context, None)


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    draft = context.user_data.pop(DRAFT, None)
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    if draft is None or query.data == f"{CB}cancel":
        await query.message.reply_text("Cadastro cancelado. Nada foi salvo.")
        return ConversationHandler.END
    conn = _db(context)
    save_draft(conn, draft, datetime.now(UTC))
    await query.message.reply_text(
        "✅ Salário cadastrado. A partir de agora o bot lança sozinho o que cair "
        "(salário, 13º, férias) e avisa.\n\n"
        + format_summary(conn, _today(context), _extra(context))
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    had = context.user_data.pop(DRAFT, None) or context.user_data.pop(EDITING, None)
    await update.effective_message.reply_text(
        "Cancelado. Nada foi salvo." if had else "Não há nada para cancelar."
    )
    return ConversationHandler.END


async def confirm_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Toque em ✅ Salvar ou ❌ Cancelar (ou /cancelar).")
    return CONFIRM


# --- edição -----------------------------------------------------------------------------

EDIT_FIELDS = {
    "raise": "Salário (reajuste)",
    "weekly_hours": "Jornada",
    "overtime_pct": "Adicional de hora extra",
    "holiday_pct": "Adicional domingo/feriado",
    "dependents": "Dependentes no IR",
    "payday_rule": "Dia do pagamento",
    "advance": "Vale",
    "closing_day": "Fechamento do ponto",
    "overtime_lag_months": "Atraso das horas extras",
    "schedule": "Datas do 13º",
    "admission": "Admissão",
    "consigned": "Consignado em folha",
    "deductions": "Outros descontos",
}
EDIT_QUESTIONS = {
    "raise": (
        "Novo salário bruto e a partir de quando. Ex.: 5.000 01/01/2027 "
        "(sem data = a partir de hoje)"
    ),
    "holiday_pct": "Adicional de domingo/feriado, em %? Ex.: 100",
    "advance": 'Vale: ex.: 40% dia 20 (ou "não" para tirar)',
    "closing_day": 'Dia de fechamento do ponto (ou "fim" para o último dia do mês)',
    "consigned": 'Nome da dívida descontada em folha (ou "nenhum")',
    "deductions": (
        "Todos os descontos fixos (substitui os atuais). "
        'Ex.: vale-transporte 6%, plano 150 (ou "nenhum")'
    ),
}


def edit_value(conn, profile, employment, field: str) -> str:
    if field == "raise":
        return _brl(profile.gross_cents)
    if field == "advance":
        return f"{profile.advance_pct}% dia {profile.advance_day}" if profile.advance_pct else "não"
    if field == "closing_day":
        return f"dia {profile.closing_day}" if profile.closing_day else "fim do mês"
    if field == "payday_rule":
        return payday_text(profile.payday_rule)
    if field == "schedule":
        return salary.format_schedule(employment.schedule)
    if field == "admission":
        return f"{employment.admission_date:%d/%m/%Y}"
    if field in ("consigned", "deductions"):
        items = [
            d for d in salary.deductions(conn) if (d.debt_id is not None) == (field == "consigned")
        ]
        return ", ".join(d.name for d in items) or "nenhum"
    return str(getattr(profile, field))


def edit_menu(conn, today: date) -> InlineKeyboardMarkup:
    profile = salary.profile_at(conn, today)
    employment = salary.get_employment(conn)
    rows = [
        [
            InlineKeyboardButton(
                f"{label}: {edit_value(conn, profile, employment, f)}",
                callback_data=f"{CB}field:{f}",
            )
        ]
        for f, label in EDIT_FIELDS.items()
    ]
    rows.append([InlineKeyboardButton("✅ Concluir", callback_data=f"{CB}done")])
    return InlineKeyboardMarkup(rows)


async def _show_menu(message: Message, context) -> int:
    await message.reply_text(
        "✏️ Salário: toque no que quer mudar", reply_markup=edit_menu(_db(context), _today(context))
    )
    return MENU


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    conn = _db(context)
    if not salary.profiles(conn):
        await message.reply_text("Cadastre o salário primeiro: /salario")
        return ConversationHandler.END
    if flows.busy(context.user_data, EDITING):
        await message.reply_text(flows.BUSY_TEXT)
        return ConversationHandler.END
    context.user_data[EDITING] = {}
    return await _show_menu(message, context)


async def on_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    field = query.data.removeprefix(f"{CB}field:")
    context.user_data[EDITING]["field"] = field
    question = EDIT_QUESTIONS.get(field) or QUESTIONS.get(field, (EDIT_FIELDS[field] + "?",))[0]
    await query.message.reply_text(
        question.replace("Pular = ", "Padrão: ") + "\n(/cancelar para sair sem mudar)"
    )
    return EDIT_ASK


def apply_edit(conn, field: str, text: str, today: date, now: datetime) -> str:
    """Aplica a edição e devolve a confirmação. FieldError se não entender."""
    norm = normalize(text.strip())
    if field == "raise":
        parts = text.split()
        gross = pd.parse_money(parts[0])
        start = pd.parse_date(parts[1], date(today.year + 2, 12, 31)) if len(parts) > 1 else today
        salary.raise_salary(conn, gross, start, now)
        return (
            f"✅ Salário de {_brl(gross)} a partir de {start:%d/%m/%Y} "
            "(o anterior fica no histórico)."
        )
    if field == "holiday_pct":
        salary.update_settings(conn, holiday_pct=st.parse_int(text, 50, 300, "adicional"))
    elif field == "advance":
        if norm in {"nao", "nenhum", "sem"}:
            salary.update_settings(conn, advance_pct=None, advance_day=None)
        else:
            pct, day = st.parse_advance(text)
            salary.update_settings(conn, advance_pct=pct, advance_day=day)
    elif field == "closing_day":
        value = (
            None if norm in {"fim", "ultimo", "ultimo dia"} else st.parse_int(text, 1, 31, "dia")
        )
        salary.update_settings(conn, closing_day=value)
    elif field in ("schedule", "admission"):
        employment = salary.get_employment(conn)
        value = parse_answer(conn, field, text, today)
        if field == "schedule":
            salary.save_employment(conn, employment.admission_date, value, now)
        else:
            salary.save_employment(conn, value, employment.schedule, now)
    elif field in ("consigned", "deductions"):
        for d in salary.deductions(conn):
            if (d.debt_id is not None) == (field == "consigned"):
                salary.remove_deduction(conn, d.id)
        if norm not in {"nenhum", "nenhuma", "nao"}:
            value = parse_answer(conn, field, text, today)
            if field == "consigned":
                salary.add_deduction(
                    conn, f"consignado {debts.get(conn, value).name}", now, debt_id=value
                )
            else:
                for spec in value:
                    salary.add_deduction(
                        conn,
                        spec.name,
                        now,
                        percent_bp=spec.percent_bp,
                        fixed_cents=spec.fixed_cents,
                    )
    else:
        salary.update_settings(conn, **{field: parse_answer(conn, field, text, today)})
    return f"✅ {EDIT_FIELDS[field]} atualizado."


async def on_edit_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    field = context.user_data[EDITING]["field"]
    try:
        text = apply_edit(
            _db(context), field, message.text or "", _today(context), datetime.now(UTC)
        )
    except (FieldError, IndexError) as exc:
        await message.reply_text(f"Não entendi: {exc}. Tente de novo ou /cancelar.")
        return EDIT_ASK
    await message.reply_text(text)
    return await _show_menu(message, context)


async def on_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    context.user_data.pop(EDITING, None)
    await query.message.reply_text(format_summary(_db(context), _today(context), _extra(context)))
    return ConversationHandler.END


async def edit_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Use os botões, ou /cancelar para sair.")
    return MENU


def _quiet(**kwargs) -> ConversationHandler:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*per_message=False.*")
        return ConversationHandler(**kwargs)


def build_conversations() -> list[ConversationHandler]:
    text = filters.TEXT & ~filters.COMMAND
    setup = _quiet(
        entry_points=[CommandHandler("salario", salary_command)],
        states={
            ASKING: [
                MessageHandler(text, on_answer),
                CallbackQueryHandler(on_skip, pattern=f"^{CB}skip$"),
            ],
            CONFIRM: [
                CallbackQueryHandler(on_confirm, pattern=f"^{CB}(save|cancel)$"),
                MessageHandler(text, confirm_hint),
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancel)],
        allow_reentry=True,
    )
    edit = _quiet(
        entry_points=[CommandHandler("editarsalario", edit_command)],
        states={
            MENU: [
                CallbackQueryHandler(on_field, pattern=f"^{CB}field:"),
                CallbackQueryHandler(on_done, pattern=f"^{CB}done$"),
                MessageHandler(text, edit_hint),
            ],
            EDIT_ASK: [MessageHandler(text, on_edit_answer)],
        },
        fallbacks=[CommandHandler("cancelar", cancel)],
        allow_reentry=True,
    )
    return [setup, edit]
