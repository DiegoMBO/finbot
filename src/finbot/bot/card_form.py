"""Roteiro do cadastro de cartão (/novocartao): perguntas, ordem e validações.

Sem nada do Telegram, para dar para testar sozinho. O rascunho é um dicionário.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from finbot.money import format_brl
from finbot.parsing import debt as p
from finbot.parsing.period import MONTH_NAMES
from finbot.services import card_math as cm
from finbot.services.credit_cards import NewCard

Draft = dict[str, Any]


@dataclass(frozen=True)
class Step:
    field: str
    optional: bool
    question: Callable[[Draft, date], str]
    parse: Callable[[str], Any]
    # Validação que depende de respostas anteriores; devolve a mensagem de erro ou None.
    check: Callable[[Draft, Any], str | None] = lambda draft, value: None


def _short(d: date) -> str:
    return f"{MONTH_NAMES[d.month - 1][:3]}/{d.year}"


def current_due(draft: Draft, today: date) -> date:
    return cm.payable_cycle(today, draft["closing_day"], draft["due_day"]).due


def next_dues(draft: Draft, today: date, n: int = 3) -> list[date]:
    first = cm.payable_cycle(today, draft["closing_day"], draft["due_day"])
    return [cm.shift(first, k, draft["closing_day"], draft["due_day"]).due for k in range(1, n + 1)]


def _check_due(draft: Draft, value: int) -> str | None:
    if value == draft.get("closing_day"):
        return "o vencimento não pode ser no mesmo dia do fechamento"
    return None


def _check_invoice(draft: Draft, value: int) -> str | None:
    if value > draft["used_cents"]:
        return f"a fatura não pode ser maior que o utilizado ({format_brl(draft['used_cents'])})"
    return None


def _check_next(draft: Draft, values: tuple[int, ...]) -> str | None:
    room = draft["used_cents"] - (draft.get("current_invoice_cents") or 0)
    if sum(values) > room:
        return f"as faturas somam mais que o utilizado restante ({format_brl(room)})"
    return None


def _next_question(draft: Draft, today: date) -> str:
    months = ", ".join(_short(d) for d in next_dues(draft, today))
    return (
        "Valores das PRÓXIMAS faturas, se o app do banco mostrar (parcelas já compradas), "
        f"em ordem: {months}...\nEx.: 600 400 250\n"
        'Se pular, esse valor fica como "sem mês definido" e você informa cada fatura '
        "quando ela fechar, com /fatura."
    )


STEPS: list[Step] = [
    Step("name", False, lambda d, t: "Nome do cartão? Ex.: Nubank", p.parse_name),
    Step(
        "closing_day",
        False,
        lambda d, t: (
            "Dia de FECHAMENTO da fatura (no app costuma aparecer como "
            '"melhor dia de compra")? 1 a 31'
        ),
        p.parse_day,
    ),
    Step(
        "due_day",
        False,
        lambda d, t: "Dia de VENCIMENTO da fatura? 1 a 31",
        p.parse_day,
        _check_due,
    ),
    Step("limit_cents", False, lambda d, t: "Limite TOTAL do cartão? Ex.: 12.000", p.parse_money),
    Step(
        "used_cents",
        False,
        lambda d, t: (
            "Quanto do limite está USADO hoje (fatura atual + parcelas futuras)? "
            "Ex.: 9.594,54 (ou 0)"
        ),
        p.parse_money_or_zero,
    ),
    Step(
        "current_invoice_cents",
        True,
        lambda d, t: f"Valor da fatura ATUAL, a que vence em {current_due(d, t):%d/%m/%Y}?",
        p.parse_money_or_zero,
        _check_invoice,
    ),
    Step("next_invoices_cents", True, _next_question, p.parse_money_list, _check_next),
    Step(
        "rate_ppm",
        True,
        lambda d, t: (
            "Taxa do ROTATIVO ao mês (aparece na fatura, ex.: 14,5)?\n"
            "Se pular, não consigo calcular os juros quando sobrar fatura sem pagar."
        ),
        p.parse_rate,
    ),
]
_BY_FIELD = {s.field: s for s in STEPS}


def step(field: str) -> Step:
    return _BY_FIELD[field]


def next_step(draft: Draft, after: str | None) -> Step | None:
    start = 0 if after is None else STEPS.index(step(after)) + 1
    for s in STEPS[start:]:
        # Sem nada usado, não há fatura a perguntar.
        if s.field in ("current_invoice_cents", "next_invoices_cents") and not draft.get(
            "used_cents"
        ):
            continue
        return s
    return None


def to_new_card(draft: Draft) -> NewCard:
    return NewCard(
        name=draft["name"],
        closing_day=draft["closing_day"],
        due_day=draft["due_day"],
        limit_cents=draft["limit_cents"],
        used_cents=draft.get("used_cents") or 0,
        current_invoice_cents=draft.get("current_invoice_cents") or None,
        next_invoices_cents=tuple(draft.get("next_invoices_cents") or ()),
        rate_ppm=draft.get("rate_ppm") or 0,
    )
