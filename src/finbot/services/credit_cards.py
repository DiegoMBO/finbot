"""Cartões de crédito: cadastro, faturas, compras, pagamentos, estornos e rotativo.

Conceitos:
- UTILIZADO (used_cents): tudo o que ainda vai ser cobrado no cartão (fatura atual
  + parcelas futuras). Compra soma, pagamento e estorno diminuem, rotativo soma
  os juros. Disponível = limite − utilizado.
- FATURA (invoices): uma por mês de vencimento. Total = soma dos itens (compras,
  parcelas, estornos, rotativo, saldo de antes do bot) ou o valor informado pelo
  banco com /fatura, que tem prioridade (o banco inclui anuidade, IOF etc.).
- "SEM MÊS DEFINIDO": utilizado − soma do que falta pagar das faturas. São
  parcelas de compras feitas antes do bot, que ele não sabe em que mês caem.
- GASTOS no /mes: compra à vista conta na data da compra; compra parcelada conta
  uma parcela por mês, na data de vencimento de cada fatura. Pagar a fatura NÃO
  é gasto (as compras já foram contadas).

Toda operação grava junto (numa transação do banco) o cartão, as faturas, os
itens, os gastos e o evento do histórico: ou entra tudo, ou nada.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from finbot.parsing.message import ParsedEntry
from finbot.parsing.text import normalize
from finbot.services import card_math as cm
from finbot.services import transactions
from finbot.services.debts import search_words
from finbot.timeutil import from_db, to_db

USER_ACTIONS = ("compra", "pagamento", "estorno")  # o que o /desfazer considera


class DuplicateCardError(ValueError):
    """Já existe um cartão com esse nome."""


class UndoBlockedError(Exception):
    """Houve outra mudança no cartão depois da ação que se quer desfazer."""


@dataclass(frozen=True)
class Card:
    id: int
    name: str
    closing_day: int
    due_day: int
    limit_cents: int
    used_cents: int
    rate_ppm: int
    created_at: datetime

    @property
    def available_cents(self) -> int:
        return self.limit_cents - self.used_cents

    @property
    def usage_bp(self) -> int:
        return cm.usage_bp(self.used_cents, self.limit_cents)


@dataclass(frozen=True)
class Invoice:
    id: int
    card_id: int
    reference_month: str
    closing_date: date
    due_date: date
    items_cents: int  # soma dos itens
    informed_total_cents: int | None  # valor do banco, se informado
    paid_cents: int
    rolled_at: datetime | None

    @property
    def total_cents(self) -> int:
        if self.informed_total_cents is not None:
            return self.informed_total_cents
        return max(self.items_cents, 0)

    @property
    def unpaid_cents(self) -> int:
        return max(self.total_cents - self.paid_cents, 0)

    @property
    def remaining_cents(self) -> int:
        """O que ainda se paga NESTA fatura (0 se o resto já foi para o rotativo)."""
        return 0 if self.rolled_at else self.unpaid_cents

    @property
    def informed(self) -> bool:
        return self.informed_total_cents is not None

    def is_closed(self, today: date) -> bool:
        return today >= self.closing_date


@dataclass(frozen=True)
class Purchase:
    id: int
    card_id: int
    description: str
    category: str
    total_cents: int
    installments: int
    cash_price_cents: int | None
    purchased_on: date


@dataclass(frozen=True)
class NewCard:
    name: str
    closing_day: int
    due_day: int
    limit_cents: int
    used_cents: int = 0
    current_invoice_cents: int | None = None
    next_invoices_cents: tuple[int, ...] = ()
    rate_ppm: int = 0


@dataclass(frozen=True)
class Allocation:
    invoice: Invoice  # já atualizada
    amount_cents: int


@dataclass(frozen=True)
class PurchaseResult:
    card: Card
    purchase: Purchase
    allocations: list[Allocation]
    used_before_cents: int
    usage_alert: int | None  # 80, 90 ou 100 (% do limite) se cruzou agora


@dataclass(frozen=True)
class PaymentResult:
    card: Card
    amount_cents: int
    allocations: list[Allocation]
    used_before_cents: int
    beyond_invoices_cents: int  # parte que não coube em nenhuma fatura conhecida


@dataclass(frozen=True)
class RefundResult:
    card: Card
    purchase: Purchase
    amount_cents: int
    removed_future_cents: int  # parcelas futuras canceladas
    credit_cents: int  # crédito lançado na fatura aberta
    credit_invoice: Invoice | None
    used_before_cents: int


@dataclass(frozen=True)
class RollResult:
    card: Card
    invoice: Invoice  # a que venceu sem ser paga inteira
    next_invoice: Invoice
    unpaid_cents: int
    interest_cents: int


@dataclass(frozen=True)
class UndoResult:
    kind: str  # "compra" ou "pagamento"
    card: Card
    amount_cents: int
    description: str = ""
    invoices: list[str] = field(default_factory=list)


# --- leitura ----------------------------------------------------------------


def name_key(name: str) -> str:
    return normalize(" ".join(name.split()))


def _card(row: sqlite3.Row) -> Card:
    return Card(
        id=row["id"],
        name=row["name"],
        closing_day=row["closing_day"],
        due_day=row["due_day"],
        limit_cents=row["limit_cents"],
        used_cents=row["used_cents"],
        rate_ppm=row["revolving_rate_ppm"],
        created_at=from_db(row["created_at"]),
    )


_INVOICE_SELECT = (
    "SELECT i.*, COALESCE((SELECT SUM(amount_cents) FROM invoice_items WHERE invoice_id = i.id), 0)"
    " AS items_cents FROM invoices i"
)


def _invoice(row: sqlite3.Row) -> Invoice:
    return Invoice(
        id=row["id"],
        card_id=row["card_id"],
        reference_month=row["reference_month"],
        closing_date=date.fromisoformat(row["closing_date"]),
        due_date=date.fromisoformat(row["due_date"]),
        items_cents=row["items_cents"],
        informed_total_cents=row["informed_total_cents"],
        paid_cents=row["paid_cents"],
        rolled_at=from_db(row["rolled_at"]) if row["rolled_at"] else None,
    )


def _purchase(row: sqlite3.Row) -> Purchase:
    return Purchase(
        id=row["id"],
        card_id=row["card_id"],
        description=row["description"],
        category=row["category"],
        total_cents=row["total_cents"],
        installments=row["installments"],
        cash_price_cents=row["cash_price_cents"],
        purchased_on=date.fromisoformat(row["purchased_on"]),
    )


def get(conn: sqlite3.Connection, card_id: int) -> Card:
    return _card(conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone())


def list_cards(conn: sqlite3.Connection) -> list[Card]:
    return [_card(r) for r in conn.execute("SELECT * FROM cards ORDER BY name_key").fetchall()]


def find(conn: sqlite3.Connection, query: str) -> list[Card]:
    """Nome exato; senão, todos cujo nome tem palavras que começam com as da busca."""
    wanted = name_key(query)
    if not wanted:
        return []
    cards = list_cards(conn)
    exact = [c for c in cards if name_key(c.name) == wanted]
    if exact:
        return exact
    words = search_words(query)
    return [
        c for c in cards if all(any(h.startswith(w) for h in search_words(c.name)) for w in words)
    ]


def mentioned_in(conn: sqlite3.Connection, words: list[str]) -> list[Card]:
    """Cartões cujas palavras do nome aparecem TODAS na mensagem."""
    present = set(words)
    return [c for c in list_cards(conn) if set(name_key(c.name).split()) <= present]


def name_taken(conn: sqlite3.Connection, name: str, except_id: int | None = None) -> bool:
    row = conn.execute("SELECT id FROM cards WHERE name_key = ?", (name_key(name),)).fetchone()
    return row is not None and row["id"] != except_id


def get_invoice(conn: sqlite3.Connection, invoice_id: int) -> Invoice:
    return _invoice(conn.execute(f"{_INVOICE_SELECT} WHERE i.id = ?", (invoice_id,)).fetchone())


def invoices(conn: sqlite3.Connection, card_id: int) -> list[Invoice]:
    rows = conn.execute(
        f"{_INVOICE_SELECT} WHERE i.card_id = ? ORDER BY i.due_date", (card_id,)
    ).fetchall()
    return [_invoice(r) for r in rows]


def invoice_for_due(conn: sqlite3.Connection, card_id: int, due: date) -> Invoice | None:
    row = conn.execute(
        f"{_INVOICE_SELECT} WHERE i.card_id = ? AND i.reference_month = ?",
        (card_id, f"{due:%Y-%m}"),
    ).fetchone()
    return _invoice(row) if row else None


def payable_cycle(card: Card, today: date) -> cm.Cycle:
    return cm.payable_cycle(today, card.closing_day, card.due_day)


def current_invoice(conn: sqlite3.Connection, card: Card, today: date) -> Invoice | None:
    """A fatura atual (a próxima a vencer), se já existir."""
    return invoice_for_due(conn, card.id, payable_cycle(card, today).due)


def overdue_invoices(conn: sqlite3.Connection, card: Card, today: date) -> list[Invoice]:
    """Faturas vencidas com algo ainda a pagar (e que não foram para o rotativo)."""
    return [i for i in invoices(conn, card.id) if i.due_date < today and i.remaining_cents > 0]


def upcoming_invoices(
    conn: sqlite3.Connection, card: Card, today: date, n: int = 4
) -> list[Invoice]:
    """A fatura atual e as seguintes que já têm algo lançado."""
    start = payable_cycle(card, today).due
    found = [i for i in invoices(conn, card.id) if i.due_date >= start]
    return found[:n]


def unallocated_cents(conn: sqlite3.Connection, card: Card) -> int:
    """Parte do utilizado que não está em nenhuma fatura conhecida."""
    owed = sum(i.remaining_cents for i in invoices(conn, card.id))
    return max(card.used_cents - owed, 0)


def purchases(conn: sqlite3.Connection, card_id: int, limit: int = 8) -> list[Purchase]:
    rows = conn.execute(
        "SELECT * FROM card_purchases WHERE card_id = ? ORDER BY id DESC LIMIT ?",
        (card_id, limit),
    ).fetchall()
    return [_purchase(r) for r in rows]


def get_purchase(conn: sqlite3.Connection, purchase_id: int) -> Purchase:
    row = conn.execute("SELECT * FROM card_purchases WHERE id = ?", (purchase_id,)).fetchone()
    return _purchase(row)


def refunded_cents(conn: sqlite3.Connection, purchase_id: int) -> int:
    """Quanto da compra já foi estornado (créditos + parcelas futuras canceladas)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM card_events"
        " WHERE purchase_id = ? AND kind = 'estorno'",
        (purchase_id,),
    ).fetchone()
    return row["s"]


def active_installment_plans(conn: sqlite3.Connection, card: Card, today: date) -> list[dict]:
    """Compras parceladas com parcelas ainda por vir: descrição, valor e quantas faltam."""
    rows = conn.execute(
        "SELECT p.id, p.description, p.installments, COUNT(it.id) AS left_count,"
        " SUM(it.amount_cents) AS left_cents FROM card_purchases p"
        " JOIN invoice_items it ON it.purchase_id = p.id AND it.kind = 'compra'"
        " JOIN invoices i ON i.id = it.invoice_id"
        " WHERE p.card_id = ? AND p.installments > 1 AND i.due_date >= ?"
        " GROUP BY p.id ORDER BY p.id",
        (card.id, payable_cycle(card, today).due.isoformat()),
    ).fetchall()
    return [dict(r) for r in rows]


# --- escrita: auxiliares ----------------------------------------------------


def _next_action_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(action_id), 0) + 1 AS n FROM card_events").fetchone()
    return row["n"]


def _event(
    conn: sqlite3.Connection,
    card_id: int,
    action_id: int,
    kind: str,
    amount: int,
    used_before: int,
    used_after: int,
    now: datetime,
    purchase_id: int | None = None,
    invoice_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO card_events (card_id, action_id, kind, amount_cents, used_before_cents,"
        " used_after_cents, purchase_id, invoice_id, occurred_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            card_id,
            action_id,
            kind,
            amount,
            used_before,
            used_after,
            purchase_id,
            invoice_id,
            to_db(now),
        ),
    )


def _set_used(conn: sqlite3.Connection, card_id: int, used: int) -> None:
    conn.execute("UPDATE cards SET used_cents = ? WHERE id = ?", (max(used, 0), card_id))


def _ensure_invoice(conn: sqlite3.Connection, card_id: int, cycle: cm.Cycle) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO invoices (card_id, reference_month, closing_date, due_date)"
        " VALUES (?, ?, ?, ?)",
        (card_id, cycle.reference_month, cycle.closing.isoformat(), cycle.due.isoformat()),
    )
    row = conn.execute(
        "SELECT id FROM invoices WHERE card_id = ? AND reference_month = ?",
        (card_id, cycle.reference_month),
    ).fetchone()
    return row["id"]


def _item(
    conn: sqlite3.Connection,
    invoice_id: int,
    kind: str,
    amount: int,
    now: datetime,
    description: str = "",
    purchase_id: int | None = None,
    installment_number: int | None = None,
    transaction_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO invoice_items (invoice_id, kind, amount_cents, purchase_id,"
        " installment_number, transaction_id, description, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            invoice_id,
            kind,
            amount,
            purchase_id,
            installment_number,
            transaction_id,
            description,
            to_db(now),
        ),
    )


# --- cadastro e edição ------------------------------------------------------


def create(conn: sqlite3.Connection, new: NewCard, today: date, now: datetime) -> Card:
    """Cadastra o cartão e as faturas informadas (atual e, se houver, as próximas)."""
    if new.limit_cents <= 0:
        raise ValueError("o limite precisa ser maior que zero")
    informed = (new.current_invoice_cents or 0) + sum(new.next_invoices_cents)
    if informed > new.used_cents:
        raise ValueError("as faturas informadas somam mais que o limite utilizado")
    if name_taken(conn, new.name):
        raise DuplicateCardError(new.name)
    with conn:
        card_id = conn.execute(
            "INSERT INTO cards (name, name_key, closing_day, due_day, limit_cents, used_cents,"
            " revolving_rate_ppm, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new.name,
                name_key(new.name),
                new.closing_day,
                new.due_day,
                new.limit_cents,
                new.used_cents,
                new.rate_ppm,
                to_db(now),
            ),
        ).lastrowid
        _event(
            conn, card_id, _next_action_id(conn), "cadastro", new.used_cents, 0, new.used_cents, now
        )
        first = cm.payable_cycle(today, new.closing_day, new.due_day)
        amounts = [new.current_invoice_cents] + list(new.next_invoices_cents)
        for k, amount in enumerate(amounts):
            if amount:
                cycle = cm.shift(first, k, new.closing_day, new.due_day)
                invoice_id = _ensure_invoice(conn, card_id, cycle)
                _item(conn, invoice_id, "anterior", amount, now, "saldo de antes do bot")
    return get(conn, card_id)


EDITABLE = {
    "name": "name",
    "closing_day": "closing_day",
    "due_day": "due_day",
    "limit_cents": "limit_cents",
    "rate_ppm": "revolving_rate_ppm",
}


def update_field(
    conn: sqlite3.Connection, card: Card, field_name: str, value, now: datetime
) -> Card:
    """Muda um dado do cartão. "used_cents" vira um ajuste registrado no histórico.

    Mudar fechamento/vencimento vale para as compras novas; faturas já criadas
    ficam com as datas que tinham (corrija uma fatura com /fatura).
    """
    if field_name == "used_cents":
        with conn:
            _set_used(conn, card.id, value)
            _event(
                conn,
                card.id,
                _next_action_id(conn),
                "ajuste",
                value - card.used_cents,
                card.used_cents,
                value,
                now,
            )
        return get(conn, card.id)
    if field_name not in EDITABLE:
        raise ValueError(f"o campo {field_name!r} não pode ser editado")
    if field_name == "name" and name_taken(conn, value, except_id=card.id):
        raise DuplicateCardError(value)
    if field_name == "rate_ppm" and value is None:
        value = 0
    with conn:
        conn.execute(f"UPDATE cards SET {EDITABLE[field_name]} = ? WHERE id = ?", (value, card.id))
        if field_name == "name":
            conn.execute("UPDATE cards SET name_key = ? WHERE id = ?", (name_key(value), card.id))
    return get(conn, card.id)


def delete(conn: sqlite3.Connection, card: Card) -> None:
    """Apaga o cartão e tudo dele. Os gastos das compras continuam no /mes."""
    with conn:
        conn.execute("DELETE FROM card_reminders WHERE card_id = ?", (card.id,))
        conn.execute("DELETE FROM card_events WHERE card_id = ?", (card.id,))
        conn.execute(
            "DELETE FROM invoice_items"
            " WHERE invoice_id IN (SELECT id FROM invoices WHERE card_id = ?)",
            (card.id,),
        )
        conn.execute("DELETE FROM card_purchases WHERE card_id = ?", (card.id,))
        conn.execute("DELETE FROM invoices WHERE card_id = ?", (card.id,))
        conn.execute("DELETE FROM cards WHERE id = ?", (card.id,))


# --- compras ----------------------------------------------------------------


def _local_noon(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time(12), tzinfo=tz)


def add_purchase(
    conn: sqlite3.Connection,
    card: Card,
    *,
    description: str,
    category: str,
    purchased_on: date,
    now: datetime,
    tz: ZoneInfo,
    total_cents: int | None = None,
    installments: int = 1,
    installment_cents: int | None = None,
    cash_price_cents: int | None = None,
) -> PurchaseResult:
    """Registra uma compra (à vista ou parcelada) nas faturas certas.

    Informe `total_cents` (dividido em parcelas iguais) OU `installment_cents`
    (valor de cada parcela, para parcelamentos com juros embutidos).
    """
    if installment_cents is not None:
        parts = [installment_cents] * installments
    elif total_cents is not None:
        parts = cm.split_installments(total_cents, installments)
    else:
        raise ValueError("informe o total ou o valor da parcela")
    total = sum(parts)
    first = cm.cycle_for_purchase(purchased_on, card.closing_day, card.due_day)
    today = now.astimezone(tz).date()
    action_id = _next_action_id(conn)

    with conn:
        purchase_id = conn.execute(
            "INSERT INTO card_purchases (card_id, description, category, total_cents, installments,"
            " cash_price_cents, purchased_on, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                card.id,
                description,
                category,
                total,
                installments,
                cash_price_cents,
                purchased_on.isoformat(),
                to_db(now),
            ),
        ).lastrowid
        invoice_ids = []
        for k, part in enumerate(parts):
            cycle = cm.shift(first, k, card.closing_day, card.due_day)
            invoice_id = _ensure_invoice(conn, card.id, cycle)
            invoice_ids.append(invoice_id)
            if installments == 1:
                # À vista: gasto na data da compra (agora, se for hoje).
                when = now if purchased_on == today else _local_noon(purchased_on, tz)
                label = description
            else:
                # Parcelada: cada parcela é gasto no mês em que a fatura vence.
                when = _local_noon(cycle.due, tz)
                label = f"{description} ({k + 1}/{installments})".strip()
            entry = ParsedEntry("expense", part, category, label)
            transaction_id = transactions.insert(conn, entry, when)
            _item(conn, invoice_id, "compra", part, now, label, purchase_id, k + 1, transaction_id)
        used_after = card.used_cents + total
        _set_used(conn, card.id, used_after)
        _event(
            conn,
            card.id,
            action_id,
            "compra",
            total,
            card.used_cents,
            used_after,
            now,
            purchase_id=purchase_id,
        )

    allocations = [
        Allocation(get_invoice(conn, invoice_id), part)
        for invoice_id, part in zip(invoice_ids, parts, strict=True)
    ]
    return PurchaseResult(
        card=get(conn, card.id),
        purchase=get_purchase(conn, purchase_id),
        allocations=allocations,
        used_before_cents=card.used_cents,
        usage_alert=cm.crossed_usage(card.used_cents, used_after, card.limit_cents),
    )


# --- pagamento da fatura ----------------------------------------------------


def pay(
    conn: sqlite3.Connection, card: Card, amount_cents: int, today: date, now: datetime
) -> PaymentResult:
    """Paga faturas: primeiro as vencidas e a atual (da mais antiga para a mais nova),
    depois, se sobrar, adianta as próximas. Libera o limite do valor pago.
    """
    if amount_cents <= 0:
        raise ValueError("o pagamento precisa ser maior que zero")
    payable_due = payable_cycle(card, today).due
    open_invoices = [i for i in invoices(conn, card.id) if i.remaining_cents > 0]
    ordered = [i for i in open_invoices if i.due_date <= payable_due] + [
        i for i in open_invoices if i.due_date > payable_due
    ]
    plan: list[tuple[int, int]] = []
    left = amount_cents
    for inv in ordered:
        if left <= 0:
            break
        part = min(left, inv.remaining_cents)
        plan.append((inv.id, part))
        left -= part

    used_after = max(card.used_cents - amount_cents, 0)
    action_id = _next_action_id(conn)
    with conn:
        for invoice_id, part in plan:
            conn.execute(
                "UPDATE invoices SET paid_cents = paid_cents + ? WHERE id = ?", (part, invoice_id)
            )
            _event(
                conn,
                card.id,
                action_id,
                "pagamento",
                part,
                card.used_cents,
                used_after,
                now,
                invoice_id=invoice_id,
            )
        if left > 0 or not plan:
            _event(
                conn,
                card.id,
                action_id,
                "pagamento",
                left if plan else amount_cents,
                card.used_cents,
                used_after,
                now,
            )
        _set_used(conn, card.id, used_after)
    return PaymentResult(
        card=get(conn, card.id),
        amount_cents=amount_cents,
        allocations=[Allocation(get_invoice(conn, i), part) for i, part in plan],
        used_before_cents=card.used_cents,
        beyond_invoices_cents=left,
    )


# --- valor real da fatura ---------------------------------------------------


def inform_invoice(
    conn: sqlite3.Connection, card: Card, due: date, total_cents: int, now: datetime
) -> tuple[Invoice, int]:
    """Grava o total que o banco cobrou nessa fatura. Devolve (fatura, ajuste no utilizado).

    Se o banco cobrou MAIS do que o bot sabia, a diferença sai primeiro do
    "sem mês definido" (parcelas antigas que caíram nesta fatura); só o que
    passar disso aumenta o utilizado (anuidade, IOF, juros...). Se cobrou MENOS,
    o utilizado diminui na mesma medida.
    """
    if total_cents < 0:
        raise ValueError("o total não pode ser negativo")
    cycle = cm.cycle_for_due(due, card.closing_day)
    with conn:
        invoice_id = _ensure_invoice(conn, card.id, cycle)
    before = get_invoice(conn, invoice_id)
    delta = total_cents - before.total_cents
    if delta > 0:
        adjustment = max(delta - unallocated_cents(conn, card), 0)
    else:
        adjustment = delta
    used_after = max(card.used_cents + adjustment, 0)
    with conn:
        conn.execute(
            "UPDATE invoices SET informed_total_cents = ? WHERE id = ?", (total_cents, invoice_id)
        )
        _set_used(conn, card.id, used_after)
        _event(
            conn,
            card.id,
            _next_action_id(conn),
            "fatura",
            delta,
            card.used_cents,
            used_after,
            now,
            invoice_id=invoice_id,
        )
    return get_invoice(conn, invoice_id), used_after - card.used_cents


# --- estorno ----------------------------------------------------------------


def refund(
    conn: sqlite3.Connection,
    card: Card,
    purchase: Purchase,
    amount_cents: int | None,
    today: date,
    now: datetime,
) -> RefundResult:
    """Estorno de uma compra. `amount_cents=None` = estorno TOTAL (do que ainda não foi estornado).

    Total: as parcelas das faturas que ainda não fecharam são canceladas (somem,
    com os gastos delas); o que já tinha sido cobrado em faturas fechadas vira
    um crédito na fatura aberta. Todos os gastos da compra saem do /mes.
    Parcial: o valor vira crédito na fatura aberta e é descontado dos gastos da
    compra, da última parcela para a primeira.
    """
    refundable = purchase.total_cents - refunded_cents(conn, purchase.id)
    if refundable <= 0:
        raise ValueError("essa compra já foi estornada inteira")
    amount = refundable if amount_cents is None else amount_cents
    if amount <= 0 or amount > refundable:
        raise ValueError("o estorno precisa ser entre R$ 0,01 e o que falta estornar")
    full = amount == refundable

    items = conn.execute(
        "SELECT it.id, it.amount_cents, it.transaction_id, i.closing_date FROM invoice_items it"
        " JOIN invoices i ON i.id = it.invoice_id"
        " WHERE it.purchase_id = ? AND it.kind = 'compra' ORDER BY it.installment_number DESC",
        (purchase.id,),
    ).fetchall()
    open_cycle = cm.cycle_for_purchase(today, card.closing_day, card.due_day)
    removed = 0
    action_id = _next_action_id(conn)
    used_after = max(card.used_cents - amount, 0)

    with conn:
        if full:
            for it in items:
                if date.fromisoformat(it["closing_date"]) > today:  # fatura ainda não fechou
                    conn.execute("DELETE FROM invoice_items WHERE id = ?", (it["id"],))
                    removed += it["amount_cents"]
            for it in items:
                if it["transaction_id"] is not None:
                    conn.execute(
                        "UPDATE invoice_items SET transaction_id = NULL WHERE transaction_id = ?",
                        (it["transaction_id"],),
                    )
                    conn.execute("DELETE FROM transactions WHERE id = ?", (it["transaction_id"],))
        else:
            to_cut = amount
            for it in items:  # da última parcela para a primeira
                if to_cut <= 0 or it["transaction_id"] is None:
                    continue
                current = transactions.get(conn, it["transaction_id"]).amount_cents
                cut = min(current, to_cut)
                if cut == current:
                    conn.execute(
                        "UPDATE invoice_items SET transaction_id = NULL WHERE id = ?", (it["id"],)
                    )
                    conn.execute("DELETE FROM transactions WHERE id = ?", (it["transaction_id"],))
                else:
                    conn.execute(
                        "UPDATE transactions SET amount_cents = ? WHERE id = ?",
                        (current - cut, it["transaction_id"]),
                    )
                to_cut -= cut
        credit = amount - removed
        credit_invoice_id = None
        if credit > 0:
            credit_invoice_id = _ensure_invoice(conn, card.id, open_cycle)
            _item(
                conn,
                credit_invoice_id,
                "estorno",
                -credit,
                now,
                f"estorno: {purchase.description}".strip(),
                purchase.id,
            )
        _set_used(conn, card.id, used_after)
        _event(
            conn,
            card.id,
            action_id,
            "estorno",
            amount,
            card.used_cents,
            used_after,
            now,
            purchase_id=purchase.id,
            invoice_id=credit_invoice_id,
        )
    return RefundResult(
        card=get(conn, card.id),
        purchase=purchase,
        amount_cents=amount,
        removed_future_cents=removed,
        credit_cents=credit,
        credit_invoice=get_invoice(conn, credit_invoice_id) if credit_invoice_id else None,
        used_before_cents=card.used_cents,
    )


# --- rotativo automático ----------------------------------------------------


def roll_overdue(
    conn: sqlite3.Connection, card: Card, today: date, now: datetime
) -> list[RollResult]:
    """Faturas que venceram sem pagamento total: o que faltou + juros do rotativo
    vão para a fatura seguinte (e os juros aumentam o utilizado)."""
    results = []
    for inv in overdue_invoices(conn, card, today):
        card = get(conn, card.id)
        unpaid = inv.unpaid_cents
        interest, carried = cm.revolving(unpaid, card.rate_ppm)
        next_cycle = cm.shift(
            cm.cycle_for_due(inv.due_date, card.closing_day), 1, card.closing_day, card.due_day
        )
        used_after = card.used_cents + interest
        with conn:
            next_id = _ensure_invoice(conn, card.id, next_cycle)
            _item(
                conn,
                next_id,
                "rotativo",
                carried,
                now,
                f"rotativo da fatura de {inv.reference_month}",
            )
            conn.execute("UPDATE invoices SET rolled_at = ? WHERE id = ?", (to_db(now), inv.id))
            _set_used(conn, card.id, used_after)
            _event(
                conn,
                card.id,
                _next_action_id(conn),
                "rotativo",
                interest,
                card.used_cents,
                used_after,
                now,
                invoice_id=next_id,
            )
        results.append(
            RollResult(
                get(conn, card.id),
                get_invoice(conn, inv.id),
                get_invoice(conn, next_id),
                unpaid,
                interest,
            )
        )
    return results


# --- desfazer ---------------------------------------------------------------


@dataclass(frozen=True)
class LastAction:
    action_id: int
    kind: str
    card_id: int
    occurred_at: datetime


def last_user_action(conn: sqlite3.Connection) -> LastAction | None:
    placeholders = ",".join("?" * len(USER_ACTIONS))
    row = conn.execute(
        f"SELECT action_id, kind, card_id, occurred_at FROM card_events"
        f" WHERE kind IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        USER_ACTIONS,
    ).fetchone()
    if row is None:
        return None
    return LastAction(row["action_id"], row["kind"], row["card_id"], from_db(row["occurred_at"]))


def action_last_transaction_id(conn: sqlite3.Connection, action: LastAction) -> int | None:
    """O maior id de gasto criado por essa ação (compras criam gastos; pagamentos não).

    Serve para desempatar com um gasto comum gravado no MESMO segundo: o id do
    banco cresce na ordem de gravação, então o maior id é o mais recente.
    """
    row = conn.execute(
        "SELECT MAX(it.transaction_id) AS m FROM invoice_items it"
        " JOIN card_events e ON e.purchase_id = it.purchase_id WHERE e.action_id = ?",
        (action.action_id,),
    ).fetchone()
    return row["m"]


def card_transaction_ids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(
        "SELECT transaction_id FROM invoice_items WHERE transaction_id IS NOT NULL"
    ).fetchall()
    return {r["transaction_id"] for r in rows}


def undo(conn: sqlite3.Connection, action: LastAction) -> UndoResult:
    """Desfaz uma compra ou um pagamento (estorno não: confirme antes de fazer)."""
    if action.kind == "estorno":
        raise ValueError("estorno não pode ser desfeito; registre a compra de novo se precisar")
    rows = conn.execute(
        "SELECT * FROM card_events WHERE action_id = ? ORDER BY id", (action.action_id,)
    ).fetchall()
    later = conn.execute(
        "SELECT 1 FROM card_events WHERE card_id = ? AND id > ? LIMIT 1",
        (action.card_id, rows[-1]["id"]),
    ).fetchone()
    if later:
        raise UndoBlockedError(get(conn, action.card_id).name)
    used_before = rows[0]["used_before_cents"]

    if action.kind == "compra":
        purchase = get_purchase(conn, rows[0]["purchase_id"])
        months = [
            r["reference_month"]
            for r in conn.execute(
                "SELECT i.reference_month FROM invoice_items it JOIN invoices i"
                " ON i.id = it.invoice_id WHERE it.purchase_id = ? ORDER BY i.due_date",
                (purchase.id,),
            )
        ]
        tx_ids = [
            r["transaction_id"]
            for r in conn.execute(
                "SELECT transaction_id FROM invoice_items WHERE purchase_id = ?"
                " AND transaction_id IS NOT NULL",
                (purchase.id,),
            )
        ]
        with conn:
            conn.execute("DELETE FROM invoice_items WHERE purchase_id = ?", (purchase.id,))
            for tx_id in tx_ids:
                conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
            conn.execute("DELETE FROM card_events WHERE action_id = ?", (action.action_id,))
            conn.execute("DELETE FROM card_purchases WHERE id = ?", (purchase.id,))
            _set_used(conn, action.card_id, used_before)
        return UndoResult(
            "compra", get(conn, action.card_id), purchase.total_cents, purchase.description, months
        )

    # pagamento
    total = sum(r["amount_cents"] for r in rows)
    with conn:
        for r in rows:
            if r["invoice_id"] is not None:
                conn.execute(
                    "UPDATE invoices SET paid_cents = paid_cents - ? WHERE id = ?",
                    (r["amount_cents"], r["invoice_id"]),
                )
        conn.execute("DELETE FROM card_events WHERE action_id = ?", (action.action_id,))
        _set_used(conn, action.card_id, used_before)
    return UndoResult("pagamento", get(conn, action.card_id), total)


# --- lembretes --------------------------------------------------------------


@dataclass(frozen=True)
class InvoiceReminder:
    card: Card
    invoice: Invoice
    already_sent: bool


def due_reminders(
    conn: sqlite3.Connection, today: date, days_before: int, include_sent: bool = False
) -> list[InvoiceReminder]:
    """Faturas com algo a pagar vencendo em até `days_before` dias."""
    found = []
    for card in list_cards(conn):
        inv = current_invoice(conn, card, today)
        if inv is None or inv.remaining_cents == 0:
            continue
        if not 0 <= (inv.due_date - today).days <= days_before:
            continue
        sent = (
            conn.execute(
                "SELECT 1 FROM card_reminders WHERE card_id = ? AND due_date = ?",
                (card.id, inv.due_date.isoformat()),
            ).fetchone()
            is not None
        )
        if include_sent or not sent:
            found.append(InvoiceReminder(card, inv, sent))
    return found


def mark_reminded(conn: sqlite3.Connection, card_id: int, due: date, now: datetime) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO card_reminders (card_id, due_date, sent_at) VALUES (?, ?, ?)",
            (card_id, due.isoformat(), to_db(now)),
        )
