"""Salário CLT: cadastro, holerite estimado, horas extras, férias, próximos
pagamentos e o lançamento automático no dia em que o dinheiro cai.

Como o bot monta o holerite da competência C (o mês trabalhado), pago em C+1:
- salário do perfil vigente, proporcional se você entrou no meio do mês ou se
  teve dias de férias em C (o salário desses dias veio adiantado com as férias);
- + horas extras que CAEM nesse pagamento (fechamento do ponto + atraso);
- − INSS (tabela do ano da competência) − IR (do ano do pagamento);
- − descontos fixos (VT, plano...) − vale (se a empresa adianta parte no mês);
- consignado: sai do líquido e vira PAGAMENTO da dívida ligada a ele.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from finbot.parsing.message import ParsedEntry
from finbot.services import debts, payroll, transactions
from finbot.services.finance import add_months
from finbot.services.payroll import Month, OvertimeMonth
from finbot.timeutil import from_db, to_db

PAYROLL_CATEGORIES = {"salário", "13º salário", "férias", "adiantamento"}
KIND_CATEGORY = {
    "salario": "salário",
    "vale": "adiantamento",
    "13-primeira": "13º salário",
    "13-segunda": "13º salário",
    "ferias": "férias",
}
KIND_LABEL = {
    "salario": "salário",
    "vale": "vale (adiantamento)",
    "13-primeira": "13º (1ª parcela)",
    "13-segunda": "13º (2ª parcela)",
    "ferias": "férias",
}
MONTHS_PT = (
    "jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez",
)  # fmt: skip


def month_label(month: Month) -> str:
    return f"{MONTHS_PT[month[1] - 1]}/{month[0]}"


# --- cadastro -------------------------------------------------------------------


@dataclass(frozen=True)
class Employment:
    admission_date: date
    schedule: list[tuple[int, int]]  # datas do 13º: [(dia, mês)]


@dataclass(frozen=True)
class Profile:
    id: int
    effective_from: date
    gross_cents: int
    weekly_hours: int
    overtime_pct: int
    holiday_pct: int
    dependents: int
    payday_rule: str
    advance_pct: int | None
    advance_day: int | None
    closing_day: int | None
    overtime_lag_months: int
    created_at: datetime

    @property
    def hourly(self) -> Decimal:
        return payroll.hourly_rate(self.gross_cents, self.weekly_hours)


@dataclass(frozen=True)
class Deduction:
    id: int
    name: str
    percent_bp: int | None
    fixed_cents: int | None
    debt_id: int | None


def parse_schedule(text: str) -> list[tuple[int, int]]:
    """'30/06, 30/11, 18/12' -> [(30, 6), (30, 11), (18, 12)] (ordenado)."""
    found = []
    for part in (p.strip() for p in text.replace(";", ",").split(",") if p.strip()):
        try:
            day, month = (int(x) for x in part.split("/"))
            date(2023, month, day)  # valida (ano não bissexto: sem 29/02)
        except ValueError as exc:
            raise ValueError(f"data inválida: {part!r} (use DD/MM)") from exc
        found.append((day, month))
    if not found:
        raise ValueError("informe ao menos uma data")
    return sorted(set(found), key=lambda dm: (dm[1], dm[0]))


def format_schedule(schedule: list[tuple[int, int]]) -> str:
    return ", ".join(f"{d:02d}/{m:02d}" for d, m in schedule)


def get_employment(conn: sqlite3.Connection) -> Employment | None:
    row = conn.execute("SELECT * FROM employment WHERE id = 1").fetchone()
    if row is None:
        return None
    return Employment(
        date.fromisoformat(row["admission_date"]), parse_schedule(row["thirteenth_schedule"])
    )


def save_employment(
    conn: sqlite3.Connection, admission: date, schedule: list[tuple[int, int]], now: datetime
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO employment (id, admission_date, thirteenth_schedule, created_at)"
            " VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET"
            " admission_date = excluded.admission_date,"
            " thirteenth_schedule = excluded.thirteenth_schedule",
            (admission.isoformat(), format_schedule(schedule), to_db(now)),
        )


def _profile(row: sqlite3.Row) -> Profile:
    return Profile(
        id=row["id"],
        effective_from=date.fromisoformat(row["effective_from"]),
        gross_cents=row["gross_cents"],
        weekly_hours=row["weekly_hours"],
        overtime_pct=row["overtime_pct"],
        holiday_pct=row["holiday_pct"],
        dependents=row["dependents"],
        payday_rule=row["payday_rule"],
        advance_pct=row["advance_pct"],
        advance_day=row["advance_day"],
        closing_day=row["closing_day"],
        overtime_lag_months=row["overtime_lag_months"],
        created_at=from_db(row["created_at"]),
    )


def profiles(conn: sqlite3.Connection) -> list[Profile]:
    rows = conn.execute("SELECT * FROM salary_profiles ORDER BY effective_from").fetchall()
    return [_profile(r) for r in rows]


def profile_at(conn: sqlite3.Connection, day: date) -> Profile | None:
    """O perfil vigente no dia (o último reajuste até lá; antes do 1º, o 1º)."""
    found = profiles(conn)
    if not found:
        return None
    valid = [p for p in found if p.effective_from <= day]
    return valid[-1] if valid else found[0]


PROFILE_FIELDS = (
    "gross_cents", "weekly_hours", "overtime_pct", "holiday_pct", "dependents", "payday_rule",
    "advance_pct", "advance_day", "closing_day", "overtime_lag_months",
)  # fmt: skip


def add_profile(conn: sqlite3.Connection, effective_from: date, now: datetime, **values) -> Profile:
    """Novo perfil (cadastro ou reajuste) valendo a partir de `effective_from`."""
    unknown = set(values) - set(PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"campos desconhecidos: {sorted(unknown)}")
    cols = ["effective_from", *values, "created_at"]
    args = [effective_from.isoformat(), *values.values(), to_db(now)]
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO salary_profiles ({', '.join(cols)})"
            f" VALUES ({', '.join('?' * len(cols))})",
            args,
        )
    return profile_at(conn, effective_from)


def raise_salary(
    conn: sqlite3.Connection, gross_cents: int, effective_from: date, now: datetime
) -> Profile:
    """Reajuste: copia a configuração vigente e troca o salário a partir da data."""
    base = profile_at(conn, effective_from)
    values = {f: getattr(base, f) for f in PROFILE_FIELDS}
    values["gross_cents"] = gross_cents
    return add_profile(conn, effective_from, now, **values)


def update_settings(conn: sqlite3.Connection, **values) -> None:
    """Muda configurações (jornada, adicional, pagamento...) em TODOS os perfis.
    Para o salário em si, use raise_salary (mantém o histórico)."""
    for name in values:
        if name not in PROFILE_FIELDS or name == "gross_cents":
            raise ValueError(f"campo não editável aqui: {name}")
    with conn:
        for name, value in values.items():
            conn.execute(f"UPDATE salary_profiles SET {name} = ?", (value,))


def deductions(conn: sqlite3.Connection) -> list[Deduction]:
    rows = conn.execute("SELECT * FROM salary_deductions ORDER BY id").fetchall()
    return [
        Deduction(r["id"], r["name"], r["percent_bp"], r["fixed_cents"], r["debt_id"]) for r in rows
    ]


def add_deduction(
    conn: sqlite3.Connection,
    name: str,
    now: datetime,
    percent_bp: int | None = None,
    fixed_cents: int | None = None,
    debt_id: int | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO salary_deductions (name, percent_bp, fixed_cents, debt_id, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, percent_bp, fixed_cents, debt_id, to_db(now)),
        )


def remove_deduction(conn: sqlite3.Connection, deduction_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM salary_deductions WHERE id = ?", (deduction_id,))


# --- horas extras e férias ------------------------------------------------------


@dataclass(frozen=True)
class OvertimeEntry:
    id: int
    worked_on: date
    minutes: int
    pct: int
    note: str


def add_overtime(
    conn: sqlite3.Connection, worked_on: date, minutes: int, pct: int, now: datetime, note: str = ""
) -> OvertimeEntry:
    with conn:
        new_id = conn.execute(
            "INSERT INTO overtime_entries (worked_on, minutes, pct, note, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (worked_on.isoformat(), minutes, pct, note, to_db(now)),
        ).lastrowid
    return OvertimeEntry(new_id, worked_on, minutes, pct, note)


def overtime_entries(conn: sqlite3.Connection) -> list[OvertimeEntry]:
    rows = conn.execute("SELECT * FROM overtime_entries ORDER BY worked_on, id").fetchall()
    return [
        OvertimeEntry(
            r["id"], date.fromisoformat(r["worked_on"]), r["minutes"], r["pct"], r["note"]
        )
        for r in rows
    ]


def last_overtime(conn: sqlite3.Connection) -> tuple[OvertimeEntry, datetime] | None:
    row = conn.execute("SELECT * FROM overtime_entries ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    entry = OvertimeEntry(
        row["id"], date.fromisoformat(row["worked_on"]), row["minutes"], row["pct"], row["note"]
    )
    return entry, from_db(row["created_at"])


def delete_overtime(conn: sqlite3.Connection, entry_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM overtime_entries WHERE id = ?", (entry_id,))


def overtime_by_competence(
    conn: sqlite3.Connection, profile: Profile, extra=()
) -> dict[Month, OvertimeMonth]:
    """Horas extras somadas por mês de ponto (competência), valoradas pelo `profile`."""
    grouped: dict[Month, dict[int, int]] = {}
    for e in overtime_entries(conn):
        comp = payroll.overtime_competence(e.worked_on, profile.closing_day)
        grouped.setdefault(comp, {})
        grouped[comp][e.pct] = grouped[comp].get(e.pct, 0) + e.minutes
    return {
        comp: payroll.overtime_month(comp, by_pct, profile.hourly, extra)
        for comp, by_pct in sorted(grouped.items())
    }


@dataclass(frozen=True)
class Vacation:
    id: int
    start_date: date
    days: int
    sold_days: int
    thirteenth_advance: bool

    @property
    def end_date(self) -> date:
        return self.start_date + timedelta(days=self.days - 1)

    @property
    def pay_date(self) -> date:
        """A CLT manda pagar até 2 dias antes de começar (art. 145)."""
        return self.start_date - timedelta(days=2)


def add_vacation(
    conn: sqlite3.Connection, start: date, days: int, sold_days: int, now: datetime,
    thirteenth_advance: bool = False,
) -> Vacation:  # fmt: skip
    if not 5 <= days <= 30 or not 0 <= sold_days <= 10 or days + sold_days > 30:
        raise ValueError("férias: 5 a 30 dias, vendendo no máximo 10, total até 30")
    with conn:
        new_id = conn.execute(
            "INSERT INTO vacations (start_date, days, sold_days, thirteenth_advance, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (start.isoformat(), days, sold_days, int(thirteenth_advance), to_db(now)),
        ).lastrowid
    return Vacation(new_id, start, days, sold_days, thirteenth_advance)


def vacations(conn: sqlite3.Connection) -> list[Vacation]:
    rows = conn.execute("SELECT * FROM vacations ORDER BY start_date").fetchall()
    return [
        Vacation(r["id"], date.fromisoformat(r["start_date"]), r["days"], r["sold_days"],
                 bool(r["thirteenth_advance"]))
        for r in rows
    ]  # fmt: skip


def delete_vacation(conn: sqlite3.Connection, vacation_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM vacations WHERE id = ?", (vacation_id,))


def variable_average(
    conn: sqlite3.Connection, profile: Profile, months: list[Month], extra=()
) -> int:
    """Média mensal das horas extras (com DSR) nos meses dados, ao valor-hora ATUAL
    (média física × valor da hora na data do pagamento, Súmula 347 TST)."""
    if not months:
        return 0
    by_comp = overtime_by_competence(conn, profile, extra)
    total = sum(by_comp[m].total_cents for m in months if m in by_comp)
    return total // len(months)


# --- holerite estimado ----------------------------------------------------------


@dataclass(frozen=True)
class Payslip:
    competence: Month  # mês trabalhado
    pay_date: date  # quando cai (mês seguinte)
    worked_days: int  # dias comerciais pagos (30 = mês cheio)
    salary_cents: int
    overtime: list[OvertimeMonth]  # horas extras que caem neste pagamento
    gross_cents: int
    inss_cents: int
    ir: payroll.IncomeTax
    deductions: list[tuple[str, int]]  # VT, plano...
    consigned: list[tuple[debts.Debt, int]]  # consignados (viram pagamento da dívida)
    advance_cents: int  # vale já pago no mês

    @property
    def deductions_cents(self) -> int:
        return sum(v for _, v in self.deductions)

    @property
    def consigned_cents(self) -> int:
        return sum(v for _, v in self.consigned)

    @property
    def net_cents(self) -> int:
        """Líquido ANTES do consignado: é o que entra como "salário" no /mes."""
        return (self.gross_cents - self.inss_cents - self.ir.tax_cents - self.deductions_cents
                - self.advance_cents)  # fmt: skip

    @property
    def received_cents(self) -> int:
        """O que cai na conta: líquido − consignado."""
        return self.net_cents - self.consigned_cents


def _month_end(month: Month) -> date:
    return date(month[0], month[1], calendar.monthrange(*month)[1])


def payslip(conn: sqlite3.Connection, competence: Month, extra=()) -> Payslip | None:
    employment = get_employment(conn)
    profile = profile_at(conn, _month_end(competence))
    if employment is None or profile is None:
        return None
    admission = employment.admission_date
    if (admission.year, admission.month) > competence:
        return None

    worked = 30
    if (admission.year, admission.month) == competence:
        worked = 30 - (admission.day - 1)  # mês comercial de 30 dias
    vacation_days = sum(
        payroll.vacation_days_in_month(v.start_date, v.days, *competence) for v in vacations(conn)
    )
    worked = max(min(worked, 30 - vacation_days), 0)
    salary = profile.gross_cents * worked // 30

    pay_month = add_months(competence[0], competence[1], 1)
    pay_date = payroll.payday(pay_month[0], pay_month[1], profile.payday_rule, extra)
    # Horas extras cujo dinheiro cai no mês deste pagamento.
    arriving = []
    for comp, ot in overtime_by_competence(conn, profile, extra).items():
        if add_months(comp[0], comp[1], profile.overtime_lag_months) == pay_month:
            arriving.append(ot)

    gross = salary + sum(o.total_cents for o in arriving)
    contribution = payroll.inss(gross, competence[0])
    tax = payroll.irrf(gross, contribution, profile.dependents, pay_date.year)

    fixed: list[tuple[str, int]] = []
    consigned: list[tuple[debts.Debt, int]] = []
    for d in deductions(conn):
        if d.debt_id is not None:
            debt = next((x for x in debts.list_active(conn) if x.id == d.debt_id), None)
            if debt and debt.payment_cents:
                consigned.append((debt, min(debt.payment_cents, debt.balance_cents)))
        elif d.percent_bp is not None:
            fixed.append((d.name, profile.gross_cents * d.percent_bp // 10_000))
        else:
            fixed.append((d.name, d.fixed_cents))
    advance = profile.gross_cents * profile.advance_pct // 100 if profile.advance_pct else 0
    return Payslip(
        competence, pay_date, worked, salary, arriving, gross, contribution, tax, fixed,
        consigned, advance,
    )  # fmt: skip


# --- próximos pagamentos --------------------------------------------------------


@dataclass(frozen=True)
class Upcoming:
    kind: str  # salario, vale, 13-primeira, 13-segunda, ferias
    reference: str  # AAAA-MM (salário/vale/13º) ou AAAA-MM-DD (férias)
    pay_date: date
    gross_cents: int
    inss_cents: int
    irrf_cents: int
    deductions_cents: int
    net_cents: int  # entra como entrada no /mes
    consigned: list[tuple[debts.Debt, int]] = field(default_factory=list)
    detail: object | None = None  # Payslip, ThirteenthPayment ou VacationPay

    @property
    def received_cents(self) -> int:
        return self.net_cents - sum(v for _, v in self.consigned)

    @property
    def label(self) -> str:
        text = KIND_LABEL[self.kind]
        if self.kind in ("salario", "vale"):
            year, month = (int(x) for x in self.reference.split("-"))
            text += f" de {month_label((year, month))}"
        return text


def _posted(conn: sqlite3.Connection) -> dict[tuple[str, str], sqlite3.Row]:
    rows = conn.execute("SELECT * FROM payroll_payments").fetchall()
    return {(r["kind"], r["reference"]): r for r in rows}


def _salary_upcoming(slip: Payslip) -> Upcoming:
    return Upcoming(
        "salario", f"{slip.competence[0]}-{slip.competence[1]:02d}", slip.pay_date,
        slip.gross_cents, slip.inss_cents, slip.ir.tax_cents,
        slip.deductions_cents + slip.advance_cents, slip.net_cents, slip.consigned, slip,
    )  # fmt: skip


def thirteenth_payments(conn: sqlite3.Connection, year: int, extra=()) -> list[Upcoming]:
    employment = get_employment(conn)
    profile = profile_at(conn, date(year, 12, 1))
    if employment is None or profile is None:
        return []
    months = [
        (year, m) for m in range(1, 12)
        if payroll.thirteenth_months(year, employment.admission_date, m)
        > payroll.thirteenth_months(year, employment.admission_date, m - 1)
    ]  # fmt: skip
    base = profile.gross_cents + variable_average(conn, profile, months, extra)
    posted = _posted(conn)
    overrides = {}
    for (kind, ref), row in posted.items():
        if kind == "13-primeira" and ref.startswith(f"{year}-") and row["status"] == "lancado":
            overrides[int(ref[5:7])] = row["net_cents"]
    # 1ª parcela sobre o salário fixo; a média das horas extras entra no acerto (2ª).
    plan = payroll.thirteenth_plan(
        year, employment.admission_date, base, employment.schedule, profile.dependents,
        overrides, first_base_cents=profile.gross_cents,
    )  # fmt: skip
    return [
        Upcoming(
            p.kind,
            f"{p.pay_date:%Y-%m}",
            p.pay_date,
            p.gross_cents,
            p.inss_cents,
            p.irrf_cents,
            0,
            p.net_cents,
            [],
            p,
        )  # fmt: skip
        for p in plan
    ]


def vacation_payment(conn: sqlite3.Connection, v: Vacation, extra=()) -> Upcoming | None:
    profile = profile_at(conn, v.start_date)
    if profile is None:
        return None
    # Média das horas extras dos 12 meses antes das férias.
    months = [add_months(v.start_date.year, v.start_date.month, -k) for k in range(12, 0, -1)]
    base = profile.gross_cents + variable_average(conn, profile, months, extra)
    pay = payroll.vacation_pay(base, v.days, v.sold_days, profile.dependents, v.start_date.year)
    return Upcoming(
        "ferias", v.start_date.isoformat(), v.pay_date, pay.gross_cents, pay.inss_cents,
        pay.irrf_cents, 0, pay.net_cents, [], pay,
    )  # fmt: skip


def payments_between(conn: sqlite3.Connection, start: date, end: date, extra=()) -> list[Upcoming]:
    """Todos os pagamentos previstos entre `start` e `end` (inclusive)."""
    employment = get_employment(conn)
    if employment is None or not profiles(conn):
        return []
    found: list[Upcoming] = []
    comp = add_months(start.year, start.month, -1)
    while date(comp[0], comp[1], 1) <= end:
        slip = payslip(conn, comp, extra)
        if slip is not None and start <= slip.pay_date <= end:
            found.append(_salary_upcoming(slip))
        profile = profile_at(conn, _month_end(comp))
        if slip is not None and profile and profile.advance_pct and profile.advance_day:
            last = calendar.monthrange(*comp)[1]
            day = date(comp[0], comp[1], min(profile.advance_day, last))
            if start <= day <= end and day >= employment.admission_date:
                amount = slip.advance_cents
                found.append(
                    Upcoming("vale", f"{comp[0]}-{comp[1]:02d}", day, amount, 0, 0, 0, amount)
                )
        comp = add_months(comp[0], comp[1], 1)
    for year in range(start.year, end.year + 1):
        found += [u for u in thirteenth_payments(conn, year, extra) if start <= u.pay_date <= end]
    for v in vacations(conn):
        up = vacation_payment(conn, v, extra)
        if up and start <= up.pay_date <= end:
            found.append(up)
    return sorted(found, key=lambda u: (u.pay_date, u.kind))


# --- lançamento automático e desfazer --------------------------------------------


def pending_posts(conn: sqlite3.Connection, today: date, extra=()) -> list[Upcoming]:
    """Pagamentos que já deveriam ter caído e ainda não foram lançados.

    Só a partir do cadastro do salário (não lança o passado) e no máximo 40 dias
    para trás (se o bot ficou desligado, recupera o que ficou para trás).
    """
    first = profiles(conn)
    if not first:
        return []
    registered = min(p.created_at for p in first).date()
    start = max(registered, today - timedelta(days=40))
    posted = _posted(conn)
    return [
        u for u in payments_between(conn, start, today, extra)
        if (u.kind, u.reference) not in posted and u.net_cents > 0
    ]  # fmt: skip


@dataclass(frozen=True)
class PostResult:
    upcoming: Upcoming
    payment_id: int
    transaction_id: int
    debt_payments: list[debts.PaymentResult]


def post(conn: sqlite3.Connection, up: Upcoming, now: datetime, tz: ZoneInfo) -> PostResult:
    """Lança o pagamento: primeiro os consignados (pagamento das dívidas), depois a
    entrada no /mes. A entrada fica por último: é ela que o /desfazer encontra."""
    when = datetime.combine(up.pay_date, time(9), tzinfo=tz)
    when = min(when, now)  # nunca no futuro
    debt_results = []
    for debt, amount in up.consigned:
        current = debts.get(conn, debt.id)
        if current.status == "ativa" and amount > 0:
            debt_results.append(debts.pay(conn, current, amount, when, tz))
    entry = ParsedEntry("income", up.net_cents, KIND_CATEGORY[up.kind], up.label)
    with conn:
        tx_id = transactions.insert(conn, entry, when)
        pay_id = conn.execute(
            "INSERT INTO payroll_payments (kind, reference, paid_on, gross_cents, inss_cents,"
            " irrf_cents, deductions_cents, net_cents, transaction_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (up.kind, up.reference, up.pay_date.isoformat(), up.gross_cents, up.inss_cents,
             up.irrf_cents, up.deductions_cents, up.net_cents, tx_id, to_db(now)),
        ).lastrowid  # fmt: skip
        for r in debt_results:
            conn.execute(
                "INSERT INTO payroll_links (payment_id, transaction_id) VALUES (?, ?)",
                (pay_id, r.transaction_id),
            )
    return PostResult(up, pay_id, tx_id, debt_results)


@dataclass(frozen=True)
class PostedPayment:
    id: int
    kind: str
    reference: str
    net_cents: int
    transaction_id: int | None
    links: list[int]


def posted_by_transaction(conn: sqlite3.Connection, transaction_id: int) -> PostedPayment | None:
    row = conn.execute(
        "SELECT * FROM payroll_payments WHERE transaction_id = ?", (transaction_id,)
    ).fetchone()
    if row is None:
        return None
    links = [
        r["transaction_id"]
        for r in conn.execute(
            "SELECT transaction_id FROM payroll_links WHERE payment_id = ? ORDER BY transaction_id",
            (row["id"],),
        )
    ]
    return PostedPayment(row["id"], row["kind"], row["reference"], row["net_cents"],
                         row["transaction_id"], links)  # fmt: skip


def undo_post(conn: sqlite3.Connection, posted: PostedPayment, tz: ZoneInfo) -> None:
    """Desfaz o lançamento: entrada + pagamentos dos consignados. Marca como
    'desfeito' para o bot não lançar de novo sozinho."""
    with conn:
        conn.execute(
            "UPDATE payroll_payments SET status = 'desfeito', transaction_id = NULL WHERE id = ?",
            (posted.id,),
        )
        conn.execute("DELETE FROM transactions WHERE id = ?", (posted.transaction_id,))
        conn.execute("DELETE FROM payroll_links WHERE payment_id = ?", (posted.id,))
    for tx_id in reversed(posted.links):  # o mais recente primeiro
        debts.undo_payment(conn, tx_id, tz)


def correct_first_thirteenth(
    conn: sqlite3.Connection, year: int, month: int, amount_cents: int, now: datetime
) -> None:
    """Grava o valor REAL que caiu numa parcela da 1ª do 13º (a 2ª se ajusta sozinha).
    Se ainda não foi lançada, lança já com o valor certo."""
    ref = f"{year}-{month:02d}"
    row = conn.execute(
        "SELECT * FROM payroll_payments WHERE kind = '13-primeira' AND reference = ?", (ref,)
    ).fetchone()
    with conn:
        if row is None:
            entry = ParsedEntry("income", amount_cents, "13º salário", "13º (1ª parcela)")
            tx_id = transactions.insert(conn, entry, now)
            conn.execute(
                "INSERT INTO payroll_payments (kind, reference, paid_on, gross_cents,"
                " net_cents, transaction_id, created_at)"
                " VALUES ('13-primeira', ?, ?, ?, ?, ?, ?)",
                (ref, now.date().isoformat(), amount_cents, amount_cents, tx_id, to_db(now)),
            )
            return
        conn.execute(
            "UPDATE payroll_payments SET gross_cents = ?, net_cents = ?, status = 'lancado'"
            " WHERE id = ?",
            (amount_cents, amount_cents, row["id"]),
        )
        if row["transaction_id"] is not None:
            conn.execute(
                "UPDATE transactions SET amount_cents = ? WHERE id = ?",
                (amount_cents, row["transaction_id"]),
            )


def monthly_net_estimate(conn: sqlite3.Connection, today: date, extra=()) -> int | None:
    """Renda base: o líquido (antes do consignado) de um mês cheio, sem horas extras."""
    profile = profile_at(conn, today)
    if profile is None or get_employment(conn) is None:
        return None
    gross = profile.gross_cents
    contribution = payroll.inss(gross, today.year)
    tax = payroll.irrf(gross, contribution, profile.dependents, today.year).tax_cents
    fixed = sum(
        profile.gross_cents * d.percent_bp // 10_000 if d.percent_bp else d.fixed_cents
        for d in deductions(conn)
        if d.debt_id is None
    )
    return gross - contribution - tax - fixed
