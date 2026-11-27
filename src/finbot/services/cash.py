"""/extrato: o extrato do mês. Entra − dívidas − faturas − gastos = saldo.

Como funciona (decisões combinadas):
- Você informa quanto tem na conta (/extrato inicial). Esse é o ponto de partida:
  tudo o que aconteceu ANTES dele já está dentro do valor informado.
- Depois do saldo inicial, cada mês junta:
  * o que JÁ aconteceu (✅): entradas e gastos registrados, pagamentos de dívidas,
    salário lançado pelo bot, pagamentos de fatura;
  * o que AINDA VAI acontecer (⏳): salário/vale/13º/férias previstos, parcelas
    das dívidas no dia do vencimento, faturas dos cartões (valor inteiro);
  * o que venceu depois do saldo inicial e não foi registrado (⚠️): conta como a pagar.
- O fim de um mês é o começo do próximo ("veio de novembro").
- Consignado: o salário aparece ANTES do desconto, e o consignado aparece como
  dívida "descontado no salário" (o saldo é o mesmo; você vê todas as dívidas).
- Gastos feitos no cartão NÃO descontam de novo: já estão na fatura.
- Dívida sem parcela (ex.: cheque especial): os juros aumentam a dívida, mas não
  saem da conta no mês. Aparecem como aviso, fora da conta.
- Sem saldo inicial: mostra só o resultado do mês (entra − sai), com o que foi
  registrado até hoje e o previsto daqui para a frente.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from finbot.parsing.period import MONTH_NAMES
from finbot.services import credit_cards as cc
from finbot.services import debts, finance, salary, transactions
from finbot.services.debts import Debt
from finbot.timeutil import from_db, to_db

MAX_MONTHS_AHEAD = 12

# Grupos do extrato, na ordem em que aparecem
INCOME, DEBT, INVOICE, EXPENSE = "entra", "divida", "fatura", "gasto"
DONE, PLANNED, LATE = "✅", "⏳", "⚠️"


@dataclass(frozen=True)
class Anchor:
    amount_cents: int
    informed_at: datetime  # UTC


@dataclass(frozen=True)
class Item:
    group: str  # INCOME, DEBT, INVOICE ou EXPENSE
    day: date | None  # None = vários dias (gastos somados por categoria)
    label: str
    amount_cents: int  # sempre positivo; o grupo diz se entra ou sai
    status: str  # DONE, PLANNED ou LATE
    note: str = ""


@dataclass(frozen=True)
class Growing:
    """Dívida sem parcela: cresce com juros, não sai da conta."""

    name: str
    balance_cents: int  # no começo do mês
    interest_cents: int  # juros do mês


@dataclass(frozen=True)
class Statement:
    year: int
    month: int
    start_cents: int | None  # None = sem saldo inicial (só o resultado do mês)
    start_label: str  # "Na conta em 27/11 (você informou)" / "Veio de novembro"
    items: list[Item] = field(default_factory=list)
    growing: list[Growing] = field(default_factory=list)
    # Parcelas dos cartões que ainda não estão em nenhuma fatura conhecida
    # ("sem mês definido"): vão cair nas próximas faturas, que podem ser maiores.
    undated_cards: list[tuple[str, int]] = field(default_factory=list)

    @property
    def undated_cents(self) -> int:
        return sum(v for _, v in self.undated_cards)

    def total(self, group: str) -> int:
        return sum(i.amount_cents for i in self.items if i.group == group)

    @property
    def result_cents(self) -> int:
        """Entra − sai, só deste mês."""
        return self.total(INCOME) - self.total(DEBT) - self.total(INVOICE) - self.total(EXPENSE)

    @property
    def end_cents(self) -> int | None:
        return None if self.start_cents is None else self.start_cents + self.result_cents


# --- saldo inicial -------------------------------------------------------------


def set_anchor(conn: sqlite3.Connection, amount_cents: int, now: datetime) -> Anchor:
    with conn:
        conn.execute(
            "INSERT INTO cash_balances (amount_cents, informed_at) VALUES (?, ?)",
            (amount_cents, to_db(now)),
        )
    return Anchor(amount_cents, from_db(to_db(now)))


def get_anchor(conn: sqlite3.Connection) -> Anchor | None:
    row = conn.execute(
        "SELECT amount_cents, informed_at FROM cash_balances ORDER BY informed_at DESC, id DESC"
        " LIMIT 1"
    ).fetchone()
    return Anchor(row["amount_cents"], from_db(row["informed_at"])) if row else None


# --- ajudantes -----------------------------------------------------------------


def _due(year: int, month: int, day: int | None) -> date:
    """Dia `day` do mês (31 em mês de 30 vira o último dia; None = último dia)."""
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day or last, last))


def _local(moment: datetime, tz: ZoneInfo) -> date:
    return moment.astimezone(tz).date()


def _ids(conn: sqlite3.Connection, sql: str) -> set[int]:
    return {r[0] for r in conn.execute(sql).fetchall()}


def _paid_in_month(conn: sqlite3.Connection, debt_id: int, start: datetime, end: datetime) -> bool:
    row = conn.execute(
        "SELECT 1 FROM debt_events WHERE debt_id = ? AND kind = 'pagamento'"
        " AND occurred_at >= ? AND occurred_at < ? LIMIT 1",
        (debt_id, to_db(start), to_db(end)),
    ).fetchone()
    return row is not None


# --- o que já aconteceu ------------------------------------------------------------


def actual_items(
    conn: sqlite3.Connection, start: datetime, end: datetime, tz: ZoneInfo
) -> list[Item]:
    """Registros com start <= data < end."""
    # Gastos das compras no cartão: já estão na fatura (não contam duas vezes).
    card_tx = _ids(
        conn, "SELECT transaction_id FROM invoice_items WHERE transaction_id IS NOT NULL"
    )
    consigned_tx = _ids(conn, "SELECT transaction_id FROM payroll_links")
    items: list[Item] = []
    expenses: dict[str, int] = {}
    for t in transactions.list_between(conn, start, end):
        if t.id in card_tx:
            continue
        day = _local(t.created_at, tz)
        if t.kind == "income":
            items.append(Item(INCOME, day, t.description or t.category, t.amount_cents, DONE))
        elif t.category == debts.DEBT_CATEGORY:
            name = t.description.removeprefix("pagamento ").strip() or t.description
            note = "descontado no salário" if t.id in consigned_tx else ""
            items.append(Item(DEBT, day, name, t.amount_cents, DONE, note))
        else:
            expenses[t.category] = expenses.get(t.category, 0) + t.amount_cents
    rows = conn.execute(
        "SELECT e.card_id, e.action_id, MIN(e.occurred_at) AS at, SUM(e.amount_cents) AS total,"
        " c.name FROM card_events e JOIN cards c ON c.id = e.card_id"
        " WHERE e.kind = 'pagamento' AND e.occurred_at >= ? AND e.occurred_at < ?"
        " GROUP BY e.card_id, e.action_id ORDER BY at",
        (to_db(start), to_db(end)),
    ).fetchall()
    for r in rows:
        day = _local(from_db(r["at"]), tz)
        items.append(Item(INVOICE, day, f"fatura {r['name']}", r["total"], DONE, "pago"))
    for category, total in sorted(expenses.items(), key=lambda kv: -kv[1]):
        items.append(Item(EXPENSE, None, category, total, DONE))
    return items


# --- o que ainda vai acontecer -------------------------------------------------------


def planned_items(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    plan_from: date,
    today: date,
    balances: dict[int, int],
    tz: ZoneInfo,
    extra=(),
) -> tuple[list[Item], list[Growing]]:
    """Itens previstos do mês com data >= `plan_from`, ainda não pagos.

    `balances` (id da dívida -> saldo projetado) é atualizado: cada parcela
    abate o saldo, os juros aumentam. Assim, a dívida some quando acabar.
    Data antes de hoje e sem registro = atrasado (⚠️).
    """
    last = _due(year, month, None)
    items: list[Item] = []
    growing: list[Growing] = []
    active = [d for d in debts.list_active(conn) if d.kind != "cartao" and d.id in balances]
    by_id: dict[int, Debt] = {d.id: d for d in active}

    def status(day: date) -> str:
        return LATE if day < today else PLANNED

    # 1. Juros do mês em todas as dívidas (antes dos pagamentos). Se já houve
    #    pagamento registrado no mês, o saldo do banco já tem os juros do mês.
    m_start, m_end = transactions.month_bounds(year, month, tz)
    paid = {d.id for d in active if _paid_in_month(conn, d.id, m_start, m_end)}
    interest: dict[int, int] = {}
    for d in active:
        rate = d.rate_ppm if debts.charges_interest(d) and d.id not in paid else 0
        interest[d.id] = finance.monthly_interest(balances[d.id], rate) if balances[d.id] else 0

    # 2. Salário, vale, 13º, férias ainda não lançados (+ o consignado de cada um)
    done_keys = {key for key, row in salary._posted(conn).items()}
    consigned_ids = {x.debt_id for x in salary.deductions(conn) if x.debt_id}
    if plan_from <= last:
        for up in salary.payments_between(conn, plan_from, last, extra):
            if (up.kind, up.reference) in done_keys or up.net_cents <= 0:
                continue
            items.append(Item(INCOME, up.pay_date, up.label, up.net_cents, status(up.pay_date)))
            for debt, amount in up.consigned:
                if debt.id not in by_id:
                    continue
                part = min(amount, balances[debt.id] + interest[debt.id])
                if part > 0:
                    items.append(
                        Item(DEBT, up.pay_date, debt.name, part, status(up.pay_date),
                             "descontado no salário")
                    )  # fmt: skip
                    balances[debt.id] -= part

    # 3. Parcelas das dívidas no dia do vencimento
    for d in active:
        balances[d.id] += interest[d.id]
        if balances[d.id] <= 0:
            continue
        if not d.payment_cents:
            if interest[d.id]:
                growing.append(Growing(d.name, balances[d.id] - interest[d.id], interest[d.id]))
            continue
        if d.id in consigned_ids:
            continue  # veio com o salário
        due = _due(year, month, d.due_day)
        amount = min(d.payment_cents, balances[d.id])
        if d.id in paid:
            continue  # pagamento registrado (o saldo da dívida já está atualizado)
        balances[d.id] -= amount
        if due >= plan_from:  # antes do saldo inicial: já está dentro do valor informado
            items.append(Item(DEBT, due, d.name, amount, status(due)))

    # 4. Faturas dos cartões que vencem no mês: o valor inteiro que falta pagar
    for card in cc.list_cards(conn):
        for inv in cc.invoices(conn, card.id):
            if not (plan_from <= inv.due_date <= last) or inv.rolled_at:
                continue
            if inv.remaining_cents > 0:
                items.append(
                    Item(INVOICE, inv.due_date, f"fatura {card.name}", inv.remaining_cents,
                         status(inv.due_date))
                )  # fmt: skip
    for d in active:
        balances[d.id] = max(balances[d.id], 0)
    return items, growing


# --- o extrato -------------------------------------------------------------------


def months_between(a: tuple[int, int], b: tuple[int, int]) -> int:
    return (b[0] - a[0]) * 12 + (b[1] - a[1])


def statement(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    today: date,
    tz: ZoneInfo,
    extra=(),
) -> Statement:
    """Extrato do mês (year, month).

    Com saldo inicial, os meses são encadeados desde o mês em que ele foi
    informado: o fim de um é o começo do outro. Sem saldo inicial, cada mês é
    calculado sozinho, a partir do mês atual.
    """
    return statements(conn, year, month, today, tz, extra)[-1]


def statements(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    today: date,
    tz: ZoneInfo,
    extra=(),
) -> list[Statement]:
    """Todos os extratos encadeados até (year, month), o último é o pedido."""
    anchor = get_anchor(conn)
    target = (year, month)
    current = (today.year, today.month)
    anchor_day = _local(anchor.informed_at, tz) if anchor else None
    base = (anchor_day.year, anchor_day.month) if anchor_day else current
    if months_between(base, target) < 0 or (anchor is None and target < current):
        # Mês passado (ou antes do saldo inicial): só o que foi registrado.
        start, end = transactions.month_bounds(year, month, tz)
        return [Statement(year, month, None, "", actual_items(conn, start, end, tz))]

    chain: list[Statement] = []
    balances = {d.id: d.balance_cents for d in debts.list_active(conn) if d.kind != "cartao"}
    undated = [
        (card.name, value)
        for card in cc.list_cards(conn)
        if (value := cc.unallocated_cents(conn, card)) > 0
    ]
    start_cents = anchor.amount_cents if anchor else None
    label = f"Na conta em {anchor_day:%d/%m} (você informou)" if anchor_day else ""
    ym = base
    while True:
        y, m = ym
        m_start, m_end = transactions.month_bounds(y, m, tz)
        if anchor and ym == base:
            # No mês do saldo inicial: só o que veio DEPOIS dele.
            actual = actual_items(conn, max(anchor.informed_at, m_start), m_end, tz)
            plan_from = anchor_day
        elif anchor is None and ym == current:
            # Sem saldo inicial: o passado do mês vem dos registros; o resto é previsto.
            actual = actual_items(conn, m_start, m_end, tz)
            plan_from = today
        else:
            actual = actual_items(conn, m_start, m_end, tz)
            plan_from = date(y, m, 1)
        planned, growing = planned_items(conn, y, m, plan_from, today, balances, tz, extra)
        items = sorted(actual + planned, key=lambda i: (i.day is None, i.day or date.max))
        result = Statement(y, m, start_cents, label, items, growing, undated)
        chain.append(result)
        if ym == target:
            return chain
        if start_cents is not None:
            start_cents = result.end_cents
            label = f"Veio de {MONTH_NAMES[m - 1]}"
        ym = finance.add_months(y, m, 1)


# --- o que já está comprometido e o ponto mais baixo -----------------------------


def _signed(item: Item) -> int:
    return item.amount_cents if item.group == INCOME else -item.amount_cents


@dataclass(frozen=True)
class Outlook:
    """O que o "fim do mês" não mostra sozinho.

    O dinheiro do salário do dia 30 já tem dono: as contas que vencem antes do
    próximo dinheiro entrar. free_cents = fim do mês − essas contas
    (negativo = "faltam X para pagar as contas até o próximo dinheiro").
    O ponto mais baixo mostra se a conta fica negativa no meio do caminho.
    """

    next_income_day: date | None  # quando entra o próximo dinheiro depois do mês
    committed: list[Item]  # contas entre o fim do mês e o próximo dinheiro
    free_cents: int  # fim do mês − comprometido
    low_day: date
    low_cents: int
    negative: list[tuple[date, date | None]]  # períodos no negativo (None = segue negativo)
    until: date  # até onde olhei

    @property
    def committed_cents(self) -> int:
        return sum(i.amount_cents for i in self.committed)


def outlook(
    conn: sqlite3.Connection, year: int, month: int, today: date, tz: ZoneInfo, extra=()
) -> Outlook | None:
    """Só faz sentido com saldo inicial e para o mês atual ou os próximos."""
    if (year, month) < (today.year, today.month):
        return None
    ny, nm = finance.add_months(year, month, 1)
    chain = statements(conn, ny, nm, today, tz, extra)
    if chain[0].start_cents is None or len(chain) < 2:
        return None
    month_stmt, next_stmt = chain[-2], chain[-1]
    month_end = _due(year, month, None)

    # 1. Saldo dia a dia. O que já aconteceu (✅) e o atrasado (⚠️) contam hoje;
    #    o previsto, no dia dele. No mesmo dia, o dinheiro entra antes de sair.
    balance = chain[0].start_cents
    events: list[tuple[date, int, int]] = []
    for s in chain:
        for i in s.items:
            when = today if i.status != PLANNED or i.day is None else i.day
            events.append((when, 0 if i.group == INCOME else 1, _signed(i)))
    events.sort()
    first_day = max(today, date(year, month, 1))
    until = _due(ny, nm, None)
    # Antes da janela: só soma. Dentro dela: um ponto por dia (saldo no fim do dia).
    points: list[tuple[date, int]] = []
    for when, _, value in events:
        if when > until:
            break
        balance += value
        if when < first_day:
            continue
        if points and points[-1][0] == when:
            points[-1] = (when, balance)
        else:
            points.append((when, balance))
    if not points or points[0][0] != first_day:
        before = (
            points[0][1] - sum(v for w, _, v in events if w == points[0][0]) if points else balance
        )
        points.insert(0, (first_day, before))
    low_day, low_cents = min(points, key=lambda p: (p[1], p[0]))

    negative: list[tuple[date, date | None]] = []
    start_neg = None
    for day, value in points:
        if value < 0 and start_neg is None:
            start_neg = day
        elif value >= 0 and start_neg is not None:
            negative.append((start_neg, day - timedelta(days=1)))
            start_neg = None
    if start_neg is not None:
        negative.append((start_neg, None))

    # 2. Comprometido: contas do mês seguinte que vencem antes do próximo dinheiro
    incomes = [i.day for i in next_stmt.items if i.group == INCOME and i.day and i.day > month_end]
    next_income = min(incomes) if incomes else None
    committed = [
        i for i in next_stmt.items
        if i.group != INCOME and i.status == PLANNED and i.day
        and (next_income is None or i.day < next_income)
    ]  # fmt: skip
    free = month_stmt.end_cents - sum(i.amount_cents for i in committed)
    return Outlook(next_income, committed, free, low_day, low_cents, negative, until)
