"""Roteiro do cadastro de dívida: quais perguntas, em que ordem, e como ler cada resposta.

Fica separado dos handlers (e sem importar nada do Telegram) para que a ordem
das perguntas e as regras possam ser testadas sozinhas. O rascunho do cadastro
é um dicionário simples: {"name": "...", "kind": "emprestimo", ...}.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from finbot.money import format_brl
from finbot.parsing import debt as p
from finbot.services.debts import NO_INSTALLMENT_KINDS, NewDebt
from finbot.services.finance import price_payment

Draft = dict[str, Any]

# Para estes tipos não existe "número de parcelas": a pergunta é pulada.
NO_INSTALLMENTS = NO_INSTALLMENT_KINDS


@dataclass(frozen=True)
class Step:
    field: str
    optional: bool
    question: Callable[[Draft], str]
    parse: Callable[[str, date], Any]


def _payment_question(draft: Draft) -> str:
    text = "Qual o valor da parcela ou do pagamento mensal? Ex.: 450"
    computed = computed_payment(draft)
    if computed is not None:
        return f"{text}\nSe pular, uso a parcela da tabela Price: {format_brl(computed)}."
    return f"{text}\nPode pular se não houver um valor fixo."


STEPS: list[Step] = [
    Step(
        "name",
        False,
        lambda d: "Qual o nome da dívida? Ex.: Empréstimo Nubank",
        lambda t, _: p.parse_name(t),
    ),
    Step("kind", False, lambda d: "Qual o tipo?", lambda t, _: p.parse_kind(t)),
    Step(
        "creditor",
        True,
        lambda d: "Qual o NOME do credor? Ex.: Nubank, Casas Bahia, Tia Rosa",
        lambda t, _: p.parse_text(t),
    ),
    Step(
        "balance_cents",
        False,
        lambda d: "Qual o saldo devedor atual? Ex.: 4.200,00",
        lambda t, _: p.parse_money(t),
    ),
    Step(
        "rate_ppm",
        True,
        lambda d: (
            "Qual a taxa de juros AO MÊS? Ex.: 2,5 (para 2,5% a.m.)\n"
            "Se souber só a anual, escreva por exemplo: 30% a.a.\n"
            "Se pular, considero sem juros."
        ),
        lambda t, _: p.parse_rate(t),
    ),
    Step(
        "remaining_installments",
        True,
        lambda d: "Quantas parcelas ainda faltam?",
        lambda t, _: p.parse_installments(t),
    ),
    Step("payment_cents", True, _payment_question, lambda t, _: p.parse_money(t)),
    Step(
        "due_day",
        True,
        lambda d: "Em que dia do mês vence? (1 a 31)",
        lambda t, _: p.parse_day(t),
    ),
    Step(
        "start_date",
        True,
        lambda d: "Quando a dívida começou? Ex.: 15/03/2025, 03/2025 ou hoje",
        p.parse_date,
    ),
]
_BY_FIELD = {s.field: s for s in STEPS}


def step(field: str) -> Step:
    return _BY_FIELD[field]


def applies(s: Step, draft: Draft) -> bool:
    if s.field == "remaining_installments":
        return draft.get("kind") not in NO_INSTALLMENTS
    return True


def next_step(draft: Draft, after: str | None) -> Step | None:
    """A próxima pergunta depois do campo `after` (None = a primeira)."""
    start = 0 if after is None else STEPS.index(step(after)) + 1
    for s in STEPS[start:]:
        if applies(s, draft):
            return s
    return None


def computed_payment(draft: Draft) -> int | None:
    """Parcela Price quando há número de parcelas mas não o valor."""
    n = draft.get("remaining_installments")
    balance = draft.get("balance_cents")
    if not n or not balance:
        return None
    return price_payment(balance, draft.get("rate_ppm") or 0, n)


def apply(draft: Draft, s: Step, value: Any) -> None:
    """Guarda a resposta (None = pulou) e preenche o que dá para deduzir."""
    draft[s.field] = value
    if s.field == "payment_cents" and value is None:
        computed = computed_payment(draft)
        if computed is not None:
            draft["payment_cents"] = computed
            draft["payment_computed"] = True


def to_new_debt(draft: Draft) -> NewDebt:
    return NewDebt(
        name=draft["name"],
        kind=draft["kind"],
        balance_cents=draft["balance_cents"],
        creditor=draft.get("creditor"),
        rate_ppm=draft.get("rate_ppm") or 0,
        payment_cents=draft.get("payment_cents"),
        due_day=draft.get("due_day"),
        remaining_installments=draft.get("remaining_installments"),
        start_date=draft.get("start_date"),
    )
