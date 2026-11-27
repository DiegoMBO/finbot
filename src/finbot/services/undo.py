"""Desfazer o último registro: a MESMA regra para o bot (/desfazer) e o app web.

Vale o que aconteceu por último entre:
- hora extra registrada;
- ação no cartão (compra ou pagamento: desfaz a ação inteira, com faturas e limite);
- salário/13º/férias lançado pelo bot (desfaz junto o consignado);
- pagamento de dívida (desfaz junto o saldo da dívida);
- gasto ou entrada comum (apaga).

Devolve o texto para mostrar ao usuário (o mesmo nos dois lugares).
"""

from __future__ import annotations

import sqlite3
from zoneinfo import ZoneInfo

from finbot.money import format_brl
from finbot.parsing.period import MONTH_NAMES
from finbot.services import credit_cards as cc
from finbot.services import debts, salary, transactions
from finbot.services.transactions import Transaction


def describe(t: Transaction) -> str:
    """Uma linha legível: "R$ 35,00 em alimentação (mercado)"."""
    text = f"{format_brl(t.amount_cents)} em {t.category}"
    return f"{text} ({t.description})" if t.description else text


def _month_short(reference_month: str) -> str:
    year, month = (int(x) for x in reference_month.split("-"))
    return f"{MONTH_NAMES[month - 1][:3]}/{year}"


def _card_action_is_newer(conn, action: cc.LastAction, last: Transaction | None) -> bool:
    """A ação no cartão aconteceu depois do último gasto comum?

    Os horários têm precisão de segundos; no empate, a compra decide pela ordem
    de gravação dos gastos (id). Sem gasto para comparar (pagamento), vale o cartão.
    """
    if last is None or action.occurred_at > last.created_at:
        return True
    if action.occurred_at < last.created_at:
        return False
    ref = cc.action_last_transaction_id(conn, action)
    return ref is None or ref > last.id


def _undo_card(conn: sqlite3.Connection, action: cc.LastAction) -> str:
    try:
        r = cc.undo(conn, action)
    except cc.UndoBlockedError as exc:
        return (
            f"A última ação foi no cartão {exc}, mas ele mudou depois disso (fatura informada, "
            "rotativo ou ajuste). Desfazer agora bagunçaria as faturas. Corrija com /fatura ou "
            "/editarcartao."
        )
    except ValueError as exc:
        return f"Não desfiz: {exc}."
    if r.kind == "compra":
        months = ", ".join(_month_short(m) for m in r.invoices)
        text = f"🗑️ Desfeito: compra de {format_brl(r.amount_cents)} no {r.card.name}"
        text += f" ({r.description})" if r.description else ""
        text += f". Saiu das faturas: {months}."
    else:
        text = f"🗑️ Desfeito: pagamento de {format_brl(r.amount_cents)} no {r.card.name}."
    return f"{text}\nUsado agora: {format_brl(r.card.used_cents)}."


def undo_last(conn: sqlite3.Connection, tz: ZoneInfo) -> str:
    card_action = cc.last_user_action(conn)
    # Gastos de compras no cartão são desfeitos pela compra, não um a um.
    last = transactions.last_excluding(conn, cc.card_transaction_ids(conn))
    # Hora extra registrada depois do último gasto/ação de cartão: apaga ela.
    overtime = salary.last_overtime(conn)
    if (
        overtime
        and (last is None or overtime[1] >= last.created_at)
        and (card_action is None or overtime[1] >= card_action.occurred_at)
    ):
        entry = overtime[0]
        salary.delete_overtime(conn, entry.id)
        return (
            f"🗑️ Apagado: {entry.minutes // 60}h{entry.minutes % 60:02d} extras de "
            f"{entry.worked_on:%d/%m/%Y}."
        )
    if card_action and _card_action_is_newer(conn, card_action, last):
        return _undo_card(conn, card_action)
    if last is None:
        return "Não há nada para desfazer."
    posted = salary.posted_by_transaction(conn, last.id)
    if posted is not None:  # salário/13º/férias lançado pelo bot (+ consignado junto)
        salary.undo_post(conn, posted, tz)
        return (
            f"🗑️ Desfeito: {salary.KIND_LABEL[posted.kind]} ({posted.reference}), "
            f"{format_brl(posted.net_cents)}"
            + (" e o consignado junto." if posted.links else ".")
            + "\nO bot não lança de novo sozinho."
        )
    try:
        undone = debts.undo_payment(conn, last.id, tz)
    except debts.UndoBlockedError as exc:
        return (
            f"O último registro é um pagamento da dívida {exc.debt_name}, mas o saldo dela "
            "mudou depois disso (correção com /saldo ou fatura nova). Desfazer agora "
            "apagaria essa mudança.\n"
            f"Corrija direto com /saldo {exc.debt_name} valor."
        )
    if undone is not None:
        debt, event = undone
        return (
            f"🗑️ Apagado: pagamento de {format_brl(event.amount_cents)} em {debt.name}.\n"
            f"Saldo voltou para {format_brl(debt.balance_cents)}."
        )
    transactions.delete(conn, last.id)
    return f"🗑️ Apagado: {describe(last)}"
