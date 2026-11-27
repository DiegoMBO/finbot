"""Cadastro e consulta de dívidas.

O saldo atual fica em `debts.balance_cents`; cada mudança de saldo também gera
uma linha em `debt_events`, e a sequência dessas linhas é o histórico usado
para mostrar a evolução (e, mais adiante, o gráfico).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from finbot.parsing.message import ParsedEntry
from finbot.parsing.text import normalize
from finbot.services import finance, transactions
from finbot.services.transactions import month_bounds
from finbot.timeutil import from_db, to_db

DEBT_CATEGORY = "dívidas"  # categoria dos gastos criados pelos pagamentos

KIND_LABELS = {
    "emprestimo": "empréstimo",
    "cartao": "cartão de crédito",
    "financiamento": "financiamento",
    "cheque_especial": "cheque especial",
    "parcelamento": "parcelamento de compra",
    "pessoa": "dívida com pessoa",
}


class DuplicateDebtError(ValueError):
    """Já existe uma dívida com esse nome."""


@dataclass(frozen=True)
class NewDebt:
    name: str
    kind: str
    balance_cents: int
    creditor: str | None = None
    rate_ppm: int = 0
    payment_cents: int | None = None
    due_day: int | None = None
    remaining_installments: int | None = None
    start_date: date | None = None


@dataclass(frozen=True)
class Debt:
    id: int
    name: str
    kind: str
    creditor: str | None
    balance_cents: int
    rate_ppm: int
    payment_cents: int | None
    due_day: int | None
    remaining_installments: int | None
    start_date: date | None
    status: str
    created_at: datetime

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]


@dataclass(frozen=True)
class DebtEvent:
    id: int
    kind: str
    amount_cents: int
    interest_cents: int
    balance_before_cents: int
    balance_after_cents: int
    occurred_at: datetime


def name_key(name: str) -> str:
    """Chave de busca: "Empréstimo  Nubank" -> "emprestimo nubank"."""
    return normalize(" ".join(name.split()))


def _debt(row: sqlite3.Row) -> Debt:
    return Debt(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        creditor=row["creditor"],
        balance_cents=row["balance_cents"],
        rate_ppm=row["monthly_rate_ppm"],
        payment_cents=row["payment_cents"],
        due_day=row["due_day"],
        remaining_installments=row["remaining_installments"],
        start_date=date.fromisoformat(row["start_date"]) if row["start_date"] else None,
        status=row["status"],
        created_at=from_db(row["created_at"]),
    )


def _event(row: sqlite3.Row) -> DebtEvent:
    return DebtEvent(
        id=row["id"],
        kind=row["kind"],
        amount_cents=row["amount_cents"],
        interest_cents=row["interest_cents"],
        balance_before_cents=row["balance_before_cents"],
        balance_after_cents=row["balance_after_cents"],
        occurred_at=from_db(row["occurred_at"]),
    )


def name_taken(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM debts WHERE name_key = ?", (name_key(name),)).fetchone()
    return row is not None


def create(conn: sqlite3.Connection, new: NewDebt, now: datetime) -> Debt:
    """Grava a dívida e o evento de "cadastro" (o primeiro ponto do histórico)."""
    if new.balance_cents <= 0:
        raise ValueError("o saldo precisa ser maior que zero")
    if name_taken(conn, new.name):
        raise DuplicateDebtError(new.name)
    with conn:  # dívida + evento: os dois entram, ou nenhum
        cursor = conn.execute(
            "INSERT INTO debts (name, name_key, kind, creditor, balance_cents, monthly_rate_ppm,"
            " payment_cents, due_day, remaining_installments, start_date, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new.name,
                name_key(new.name),
                new.kind,
                new.creditor,
                new.balance_cents,
                new.rate_ppm,
                new.payment_cents,
                new.due_day,
                new.remaining_installments,
                new.start_date.isoformat() if new.start_date else None,
                to_db(now),
            ),
        )
        conn.execute(
            "INSERT INTO debt_events (debt_id, kind, amount_cents, balance_before_cents,"
            " balance_after_cents, occurred_at) VALUES (?, 'cadastro', ?, 0, ?, ?)",
            (cursor.lastrowid, new.balance_cents, new.balance_cents, to_db(now)),
        )
    return get(conn, cursor.lastrowid)


# Campos que /editardivida pode mudar -> coluna no banco. O saldo NÃO está aqui
# de propósito: ele só muda por pagamento, /saldo ou fatura, que deixam histórico.
EDITABLE_COLUMNS = {
    "name": "name",
    "kind": "kind",
    "creditor": "creditor",
    "rate_ppm": "monthly_rate_ppm",
    "payment_cents": "payment_cents",
    "due_day": "due_day",
    "remaining_installments": "remaining_installments",
    "start_date": "start_date",
}
NO_INSTALLMENT_KINDS = {"cartao", "cheque_especial"}


def update_field(conn: sqlite3.Connection, debt: Debt, field: str, value) -> Debt:
    """Muda UM campo da dívida (veja EDITABLE_COLUMNS) e devolve a dívida atualizada."""
    if field not in EDITABLE_COLUMNS:
        raise ValueError(f"o campo {field!r} não pode ser editado")
    if field == "name":
        if name_key(value) != name_key(debt.name) and name_taken(conn, value):
            raise DuplicateDebtError(value)
    if field == "kind" and debt.kind == "cartao" and value != "cartao":
        has_statements = conn.execute(
            "SELECT 1 FROM card_statements WHERE debt_id = ? LIMIT 1", (debt.id,)
        ).fetchone()
        if has_statements:
            raise ValueError("esse cartão já tem faturas registradas; não dá para trocar o tipo")
    if field == "rate_ppm" and value is None:
        value = 0  # "sem juros"
    if field == "start_date" and value is not None:
        value = value.isoformat()

    column = EDITABLE_COLUMNS[field]  # vem do dicionário acima, nunca do usuário
    with conn:
        conn.execute(f"UPDATE debts SET {column} = ? WHERE id = ?", (value, debt.id))
        if field == "name":
            conn.execute("UPDATE debts SET name_key = ? WHERE id = ?", (name_key(value), debt.id))
        if field == "kind" and value in NO_INSTALLMENT_KINDS:
            # Cartão e cheque especial não têm "parcelas restantes".
            conn.execute("UPDATE debts SET remaining_installments = NULL WHERE id = ?", (debt.id,))
    return get(conn, debt.id)


def delete(conn: sqlite3.Connection, debt: Debt) -> None:
    """Apaga a dívida e tudo o que é só dela (histórico, faturas antigas, lembretes).

    Os GASTOS criados pelos pagamentos ficam: eles aconteceram e continuam
    no /mes e no /exportar.
    """
    with conn:
        conn.execute("DELETE FROM debt_reminders WHERE debt_id = ?", (debt.id,))
        conn.execute("DELETE FROM debt_events WHERE debt_id = ?", (debt.id,))
        conn.execute("DELETE FROM card_statements WHERE debt_id = ?", (debt.id,))
        conn.execute("DELETE FROM debts WHERE id = ?", (debt.id,))


def list_all(conn: sqlite3.Connection) -> list[Debt]:
    """Todas as dívidas, inclusive as quitadas."""
    rows = conn.execute("SELECT * FROM debts ORDER BY status, name_key").fetchall()
    return [_debt(r) for r in rows]


def get(conn: sqlite3.Connection, debt_id: int) -> Debt:
    return _debt(conn.execute("SELECT * FROM debts WHERE id = ?", (debt_id,)).fetchone())


def list_active(conn: sqlite3.Connection) -> list[Debt]:
    """Dívidas ativas, da maior taxa para a menor (a ordem que a avalanche sugere)."""
    rows = conn.execute(
        "SELECT * FROM debts WHERE status = 'ativa' ORDER BY monthly_rate_ppm DESC, name_key"
    ).fetchall()
    return [_debt(r) for r in rows]


def events(conn: sqlite3.Connection, debt_id: int) -> list[DebtEvent]:
    """Histórico completo, do mais antigo para o mais novo."""
    rows = conn.execute(
        "SELECT * FROM debt_events WHERE debt_id = ? ORDER BY occurred_at, id", (debt_id,)
    ).fetchall()
    return [_event(r) for r in rows]


def find(conn: sqlite3.Connection, query: str, include_closed: bool = False) -> list[Debt]:
    """Procura dívidas ativas pelo nome ou credor, sem acento e sem maiúsculas.

    1. nome exatamente igual -> só ela;
    2. senão, todas cujo nome+credor contêm TODAS as palavras da busca
       ("nubank" acha "Empréstimo Nubank"). Mais de uma = ambíguo; quem chama decide.
    """
    wanted = name_key(query)
    if not wanted:
        return []
    debts = list_all(conn) if include_closed else list_active(conn)
    exact = [d for d in debts if name_key(d.name) == wanted]
    if exact:
        return exact
    words = wanted.split()

    def haystack(d: Debt) -> list[str]:
        return name_key(f"{d.name} {d.creditor or ''}").split()

    return [d for d in debts if all(any(h.startswith(w) for h in haystack(d)) for w in words)]


# --- pagamentos, ajustes e desfazer -----------------------------------------


class UndoBlockedError(Exception):
    """O pagamento não é o último evento da dívida (houve ajuste depois)."""

    def __init__(self, debt_name: str) -> None:
        super().__init__(debt_name)
        self.debt_name = debt_name


@dataclass(frozen=True)
class PaymentResult:
    debt: Debt  # já com o saldo novo
    transaction_id: int
    amount_cents: int
    interest_cents: int  # juros do mês lançados antes de abater (0 se não houve)
    balance_before_cents: int  # saldo ANTES dos juros e do pagamento
    overpaid_cents: int  # quanto o pagamento passou do saldo devido

    @property
    def balance_after_cents(self) -> int:
        return self.debt.balance_cents

    @property
    def amortization_cents(self) -> int:
        """Quanto do pagamento reduziu a dívida (negativo: nem cobriu os juros)."""
        return self.amount_cents - self.interest_cents - self.overpaid_cents

    @property
    def paid_off(self) -> bool:
        return self.debt.status == "quitada"


def _month_window(moment: datetime, tz: ZoneInfo) -> tuple[str, str]:
    local = moment.astimezone(tz)
    start, end = month_bounds(local.year, local.month, tz)
    return to_db(start), to_db(end)


def _payments_in_month(
    conn: sqlite3.Connection, debt_id: int, moment: datetime, tz: ZoneInfo
) -> list[int]:
    start, end = _month_window(moment, tz)
    rows = conn.execute(
        "SELECT id FROM debt_events WHERE debt_id = ? AND kind = 'pagamento'"
        " AND occurred_at >= ? AND occurred_at < ?",
        (debt_id, start, end),
    ).fetchall()
    return [r["id"] for r in rows]


def charges_interest(debt: Debt) -> bool:
    """Dívidas antigas do tipo cartão (de antes do módulo de cartões) não acumulam
    juros no pagamento. Cartões novos ficam em services/credit_cards.py."""
    return debt.kind != "cartao"


# Dívidas "rotativas": zerar o saldo não encerra a dívida (vem fatura nova,
# o limite do cheque especial continua lá). Ficam ativas, "em dia".
REVOLVING = {"cartao", "cheque_especial"}


def _status_for(debt: Debt, balance_cents: int, now: datetime) -> tuple[str, str | None]:
    if balance_cents == 0 and debt.kind not in REVOLVING:
        return "quitada", to_db(now)
    return "ativa", None


def pay(
    conn: sqlite3.Connection, debt: Debt, amount_cents: int, now: datetime, tz: ZoneInfo
) -> PaymentResult:
    """Registra um pagamento: juros do mês (se for o 1º do mês), abate o saldo,
    cria o gasto em "dívidas" e o evento do histórico, tudo numa transação só.

    Modelo (aprovado no plano): no PRIMEIRO pagamento de cada mês o saldo recebe
    os juros do mês e então o pagamento abate:  saldo' = saldo × (1 + i) − pagamento.
    Pagamentos extras no mesmo mês abatem direto (os juros do mês já entraram).
    O mês é o do seu fuso horário (TIMEZONE).
    """
    if amount_cents <= 0:
        raise ValueError("o pagamento precisa ser maior que zero")
    if debt.status != "ativa":
        raise ValueError("essa dívida já está quitada")

    first_of_month = not _payments_in_month(conn, debt.id, now, tz)
    interest = 0
    if first_of_month and charges_interest(debt):
        interest = finance.monthly_interest(debt.balance_cents, debt.rate_ppm)
    due = debt.balance_cents + interest
    after = max(due - amount_cents, 0)
    overpaid = max(amount_cents - due, 0)

    installments = debt.remaining_installments
    if installments:  # None ou 0 ficam como estão
        if after == 0:
            installments = 0
        elif first_of_month:
            installments -= 1  # a parcela do mês foi paga
    status, closed_at = _status_for(debt, after, now)

    entry = ParsedEntry("expense", amount_cents, DEBT_CATEGORY, f"pagamento {debt.name}")
    with conn:  # gasto + evento + saldo: entram todos, ou nenhum
        transaction_id = transactions.insert(conn, entry, now)
        conn.execute(
            "INSERT INTO debt_events (debt_id, kind, amount_cents, interest_cents,"
            " balance_before_cents, balance_after_cents, transaction_id, occurred_at)"
            " VALUES (?, 'pagamento', ?, ?, ?, ?, ?, ?)",
            (
                debt.id,
                amount_cents,
                interest,
                debt.balance_cents,
                after,
                transaction_id,
                to_db(now),
            ),
        )
        conn.execute(
            "UPDATE debts SET balance_cents = ?, remaining_installments = ?, status = ?,"
            " closed_at = ? WHERE id = ?",
            (after, installments, status, closed_at, debt.id),
        )
    return PaymentResult(
        debt=get(conn, debt.id),
        transaction_id=transaction_id,
        amount_cents=amount_cents,
        interest_cents=interest,
        balance_before_cents=debt.balance_cents,
        overpaid_cents=overpaid,
    )


def set_balance(
    conn: sqlite3.Connection, debt: Debt, new_balance_cents: int, now: datetime
) -> tuple[Debt, int]:
    """Corrige o saldo pelo valor do app do banco. Devolve (dívida, diferença)."""
    if new_balance_cents < 0:
        raise ValueError("o saldo não pode ser negativo")
    diff = new_balance_cents - debt.balance_cents
    status, closed_at = _status_for(debt, new_balance_cents, now)
    with conn:
        conn.execute(
            "INSERT INTO debt_events (debt_id, kind, amount_cents, balance_before_cents,"
            " balance_after_cents, occurred_at) VALUES (?, 'ajuste', ?, ?, ?, ?)",
            (debt.id, diff, debt.balance_cents, new_balance_cents, to_db(now)),
        )
        conn.execute(
            "UPDATE debts SET balance_cents = ?, status = ?, closed_at = ? WHERE id = ?",
            (new_balance_cents, status, closed_at, debt.id),
        )
    return get(conn, debt.id), diff


def undo_payment(
    conn: sqlite3.Connection, transaction_id: int, tz: ZoneInfo
) -> tuple[Debt, DebtEvent] | None:
    """Se o gasto `transaction_id` é um pagamento de dívida, desfaz tudo junto:
    apaga o gasto e o evento e devolve o saldo (e as parcelas) ao que eram.

    Devolve None se o gasto não é pagamento de dívida.
    Só desfaz se o pagamento for o ÚLTIMO evento da dívida: se houve um ajuste
    manual depois, "voltar o saldo" apagaria esse ajuste sem você perceber.
    """
    row = conn.execute(
        "SELECT * FROM debt_events WHERE transaction_id = ?", (transaction_id,)
    ).fetchone()
    if row is None:
        return None
    event = _event(row)
    debt = get(conn, row["debt_id"])
    latest = conn.execute(
        "SELECT id FROM debt_events WHERE debt_id = ? ORDER BY occurred_at DESC, id DESC LIMIT 1",
        (debt.id,),
    ).fetchone()
    if latest["id"] != event.id:
        raise UndoBlockedError(debt.name)

    # As parcelas restantes só diminuíram se este foi o 1º pagamento do mês.
    others = [i for i in _payments_in_month(conn, debt.id, event.occurred_at, tz) if i != event.id]
    installments = debt.remaining_installments
    if installments is not None and not others and debt.status == "ativa":
        installments += 1
    # Se este pagamento quitou a dívida, não dá para saber quantas parcelas havia:
    # recalculamos pelo saldo restaurado quando o contrato tinha parcelas.
    if debt.status == "quitada" and installments is not None:
        restored = finance.project(event.balance_before_cents, debt.rate_ppm, debt.payment_cents)
        installments = restored.months if restored.months is not None else installments

    with conn:
        conn.execute("DELETE FROM debt_events WHERE id = ?", (event.id,))
        conn.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
        if row["statement_id"] is not None:  # cartão: a fatura volta a dever esse valor
            conn.execute(
                "UPDATE card_statements SET paid_cents = paid_cents - ? WHERE id = ?",
                (event.amount_cents, row["statement_id"]),
            )
        conn.execute(
            "UPDATE debts SET balance_cents = ?, remaining_installments = ?, status = 'ativa',"
            " closed_at = NULL WHERE id = ?",
            (event.balance_before_cents, installments, debt.id),
        )
    return get(conn, debt.id), event


# Palavras que, numa mensagem de pagamento, indicam o TIPO da dívida.
KIND_WORDS = {
    "emprestimo": {"emprestimo"},
    "cartao": {"cartao", "fatura"},
    "financiamento": {"financiamento"},
    "cheque_especial": {"cheque", "especial"},
    "parcelamento": {"parcelamento"},
    "pessoa": set(),
}


def _words_of(debt: Debt) -> set[str]:
    """Todas as palavras que "explicam" esta dívida numa mensagem."""
    words = set(name_key(debt.name).split())
    if debt.creditor:
        words |= set(name_key(debt.creditor).split())
    return words | KIND_WORDS[debt.kind]


def match_payment(conn: sqlite3.Connection, words: list[str]) -> list[Debt]:
    """Quais dívidas uma mensagem de pagamento menciona, em ordem de confiança:

    1. todas as palavras do NOME aparecem ("empréstimo nubank");
    2. senão, todas as palavras do CREDOR aparecem ("nubank");
    3. senão, uma palavra do TIPO aparece ("empréstimo", "cartão", "fatura").
    O primeiro critério que achar alguma dívida decide.
    """
    present = set(words)
    active = list_active(conn)
    by_name = [d for d in active if set(name_key(d.name).split()) <= present]
    if by_name:
        return by_name
    by_creditor = [d for d in active if d.creditor and set(name_key(d.creditor).split()) <= present]
    if by_creditor:
        return by_creditor
    return [d for d in active if KIND_WORDS[d.kind] & present]


def unexplained_words(debt: Debt, words: list[str]) -> list[str]:
    """Palavras da mensagem que não são da dívida. Ex.: em "paguei 100 de gasolina
    do carro", "gasolina" sobra: talvez seja um gasto comum, e não a dívida "Carro"."""
    known = _words_of(debt)
    return [w for w in words if w not in known]


def projection(debt: Debt, extra_cents: int = 0) -> finance.Projection:
    return finance.project(debt.balance_cents, debt.rate_ppm, debt.payment_cents, extra_cents)


def contract_mismatch(debt: Debt) -> int | None:
    """Parcelas que a CONTA dá, se forem diferentes das informadas no contrato.

    Ex.: você diz que faltam 10 parcelas, mas com esse saldo, taxa e parcela
    seriam 14: provavelmente o saldo ou a taxa estão desatualizados.
    Tolerância de 1 parcela (a última costuma ser diferente).
    """
    if debt.remaining_installments is None:
        return None
    months = projection(debt).months
    if months is None or abs(months - debt.remaining_installments) <= 1:
        return None
    return months
