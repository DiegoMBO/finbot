"""Faturas de cartão de crédito: uma por mês, cada uma tratada separadamente.

Regras:
- O saldo do cartão é o que falta pagar da fatura ATUAL (a de vencimento mais
  recente). Ao registrar a fatura do mês seguinte, o total dela já inclui o que
  ficou no rotativo (é assim que o banco cobra), então ela passa a ser o saldo.
- Os pagamentos do cartão abatem a fatura atual (veja debts.pay).
- Pagar menos que o total deixa o resto no rotativo: o bot avisa com destaque.

Este módulo não importa services/debts (debts é que importa este), para não
haver import circular; por isso escreve direto nas tabelas.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from finbot.services import finance
from finbot.timeutil import to_db


@dataclass(frozen=True)
class Statement:
    id: int
    debt_id: int
    reference_month: str  # AAAA-MM (o mês do vencimento)
    due_date: date
    total_cents: int
    minimum_cents: int | None
    paid_cents: int

    @property
    def remaining_cents(self) -> int:
        return max(self.total_cents - self.paid_cents, 0)

    @property
    def fully_paid(self) -> bool:
        return self.paid_cents >= self.total_cents

    @property
    def below_minimum(self) -> bool:
        return self.minimum_cents is not None and self.paid_cents < self.minimum_cents


@dataclass(frozen=True)
class RevolvingCost:
    """Quanto custa deixar `amount` no rotativo por um mês."""

    amount_cents: int
    rate_ppm: int
    interest_cents: int  # juros de 1 mês
    annual_ppm: int  # taxa anual equivalente


def _statement(row: sqlite3.Row) -> Statement:
    return Statement(
        id=row["id"],
        debt_id=row["debt_id"],
        reference_month=row["reference_month"],
        due_date=date.fromisoformat(row["due_date"]),
        total_cents=row["total_cents"],
        minimum_cents=row["minimum_cents"],
        paid_cents=row["paid_cents"],
    )


def current(conn: sqlite3.Connection, debt_id: int) -> Statement | None:
    row = conn.execute(
        "SELECT * FROM card_statements WHERE debt_id = ? ORDER BY reference_month DESC LIMIT 1",
        (debt_id,),
    ).fetchone()
    return _statement(row) if row else None


def get(conn: sqlite3.Connection, statement_id: int) -> Statement:
    row = conn.execute("SELECT * FROM card_statements WHERE id = ?", (statement_id,)).fetchone()
    return _statement(row)


def history(conn: sqlite3.Connection, debt_id: int) -> list[Statement]:
    rows = conn.execute(
        "SELECT * FROM card_statements WHERE debt_id = ? ORDER BY reference_month", (debt_id,)
    ).fetchall()
    return [_statement(r) for r in rows]


def register(
    conn: sqlite3.Connection,
    debt_id: int,
    total_cents: int,
    minimum_cents: int | None,
    due_date: date,
    now: datetime,
) -> Statement:
    """Registra a fatura do mês do vencimento (ou corrige, se já existir).

    Corrigir mantém o que já foi pago dela. O saldo do cartão vira o que falta
    pagar desta fatura, e o histórico ganha um evento "fatura".
    """
    if total_cents < 0:
        raise ValueError("o total não pode ser negativo")
    if minimum_cents is not None and minimum_cents > total_cents:
        raise ValueError("o mínimo não pode ser maior que o total")
    latest = current(conn, debt_id)
    month = f"{due_date:%Y-%m}"
    if latest and month < latest.reference_month:
        raise ValueError(
            f"já existe uma fatura mais nova (vencimento em {latest.due_date:%d/%m/%Y})"
        )

    balance_before = conn.execute(
        "SELECT balance_cents FROM debts WHERE id = ?", (debt_id,)
    ).fetchone()["balance_cents"]
    with conn:
        existing = conn.execute(
            "SELECT id, paid_cents FROM card_statements WHERE debt_id = ? AND reference_month = ?",
            (debt_id, month),
        ).fetchone()
        if existing:
            statement_id, paid = existing["id"], existing["paid_cents"]
            conn.execute(
                "UPDATE card_statements SET total_cents = ?, minimum_cents = ?, due_date = ?"
                " WHERE id = ?",
                (total_cents, minimum_cents, due_date.isoformat(), statement_id),
            )
        else:
            paid = 0
            statement_id = conn.execute(
                "INSERT INTO card_statements (debt_id, reference_month, due_date, total_cents,"
                " minimum_cents) VALUES (?, ?, ?, ?, ?)",
                (debt_id, month, due_date.isoformat(), total_cents, minimum_cents),
            ).lastrowid
        balance = max(total_cents - paid, 0)
        conn.execute(
            "INSERT INTO debt_events (debt_id, kind, amount_cents, balance_before_cents,"
            " balance_after_cents, statement_id, occurred_at) VALUES (?, 'fatura', ?, ?, ?, ?, ?)",
            (debt_id, total_cents, balance_before, balance, statement_id, to_db(now)),
        )
        conn.execute(
            "UPDATE debts SET balance_cents = ?, status = 'ativa', closed_at = NULL WHERE id = ?",
            (balance, debt_id),
        )
    return get(conn, statement_id)


def revolving_cost(amount_cents: int, rate_ppm: int) -> RevolvingCost:
    return RevolvingCost(
        amount_cents=amount_cents,
        rate_ppm=rate_ppm,
        interest_cents=finance.monthly_interest(amount_cents, rate_ppm),
        annual_ppm=finance.annual_equivalent_ppm(rate_ppm),
    )
