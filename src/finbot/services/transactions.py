"""Gravar, listar e apagar transações (gastos e entradas).

Decisão sobre datas: guardamos em UTC e só convertemos para o fuso do usuário
na hora de mostrar ou de calcular "hoje"/"este mês". Assim o banco nunca fica
ambíguo (ex.: mudança de horário de verão, ou trocar o TIMEZONE no .env).
As funções recebem `now` como parâmetro em vez de chamar datetime.now() por
dentro: nos testes passamos uma data fixa e o resultado é sempre o mesmo.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from finbot.parsing.message import Kind, ParsedEntry
from finbot.timeutil import from_db
from finbot.timeutil import to_db as _to_db


@dataclass(frozen=True)
class Transaction:
    id: int
    kind: Kind
    amount_cents: int
    category: str
    description: str
    created_at: datetime  # sempre com fuso (UTC)


@dataclass(frozen=True)
class Summary:
    expense_cents: int
    income_cents: int
    expenses_by_category: dict[str, int]  # da maior para a menor

    @property
    def balance_cents(self) -> int:
        return self.income_cents - self.expense_cents


def _from_row(row: sqlite3.Row) -> Transaction:
    return Transaction(
        id=row["id"],
        kind=row["kind"],
        amount_cents=row["amount_cents"],
        category=row["category"],
        description=row["description"],
        created_at=from_db(row["created_at"]),
    )


def insert(conn: sqlite3.Connection, entry: ParsedEntry, now: datetime) -> int:
    """INSERT sem commit: para quem precisa gravar junto com outras tabelas
    numa transação só (ex.: pagamento de dívida). Devolve o id criado."""
    # Os "?" são preenchidos pelo sqlite3. NUNCA monte SQL com f-string usando
    # texto do usuário: isso abre brecha para SQL injection.
    cursor = conn.execute(
        "INSERT INTO transactions (kind, amount_cents, category, description, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (entry.kind, entry.amount_cents, entry.category, entry.description, _to_db(now)),
    )
    return cursor.lastrowid


def get(conn: sqlite3.Connection, transaction_id: int) -> Transaction:
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    return _from_row(row)


def add(conn: sqlite3.Connection, entry: ParsedEntry, now: datetime) -> Transaction:
    # "with conn" abre uma transação: confirma (commit) no fim, ou desfaz se der erro.
    with conn:
        transaction_id = insert(conn, entry, now)
    return get(conn, transaction_id)


def last(conn: sqlite3.Connection) -> Transaction | None:
    row = conn.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
    return _from_row(row) if row else None


def list_between(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[Transaction]:
    """Transações com start <= created_at < end, da mais antiga para a mais nova."""
    rows = conn.execute(
        "SELECT * FROM transactions WHERE created_at >= ? AND created_at < ? ORDER BY id",
        (_to_db(start), _to_db(end)),
    ).fetchall()
    return [_from_row(r) for r in rows]


def list_all(conn: sqlite3.Connection) -> list[Transaction]:
    rows = conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()
    return [_from_row(r) for r in rows]


def delete_last(conn: sqlite3.Connection) -> Transaction | None:
    """Apaga o registro mais recente e o devolve (None se não houver nenhum)."""
    row = conn.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    with conn:
        conn.execute("DELETE FROM transactions WHERE id = ?", (row["id"],))
    return _from_row(row)


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Início e fim (exclusivo) de um dia no fuso do usuário."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    # Soma 1 dia no calendário e recombina, para acertar dias com 23h ou 25h.
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start, end


def month_bounds(year: int, month: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Início do dia 1 do mês e início do dia 1 do mês seguinte, no fuso do usuário."""
    start = datetime(year, month, 1, tzinfo=tz)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    end = datetime(next_year, next_month, 1, tzinfo=tz)
    return start, end


def summarize(items: list[Transaction]) -> Summary:
    by_category: dict[str, int] = defaultdict(int)
    expense = income = 0
    for t in items:
        if t.kind == "income":
            income += t.amount_cents
        else:
            expense += t.amount_cents
            by_category[t.category] += t.amount_cents
    ordered = dict(sorted(by_category.items(), key=lambda kv: kv[1], reverse=True))
    return Summary(expense_cents=expense, income_cents=income, expenses_by_category=ordered)
