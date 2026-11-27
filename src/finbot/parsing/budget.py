"""Interpretação dos argumentos do /orcamento.

/orcamento                      -> lista os orçamentos e o uso no mês
/orcamento alimentação 800      -> define (ou troca) o limite mensal
/orcamento comida fora 300,50   -> a categoria pode ter mais de uma palavra
/orcamento remover alimentação  -> apaga o orçamento
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from finbot.money import AmountError, parse_amount
from finbot.parsing.text import normalize

USAGE = (
    "Use:\n"
    "/orcamento: mostra os orçamentos\n"
    "/orcamento alimentação 800: define o limite mensal\n"
    "/orcamento remover alimentação: apaga o orçamento"
)


class BudgetArgsError(ValueError):
    """Argumentos inválidos para /orcamento."""


@dataclass(frozen=True)
class BudgetCommand:
    action: Literal["list", "set", "remove"]
    category: str = ""
    limit_cents: int = 0


def parse_budget_args(args: list[str]) -> BudgetCommand:
    if not args:
        return BudgetCommand("list")

    if normalize(args[0]) in {"remover", "apagar", "excluir"}:
        category = " ".join(args[1:]).lstrip("#").strip()
        if not category:
            raise BudgetArgsError("faltou a categoria")
        return BudgetCommand("remove", category)

    if len(args) < 2:
        raise BudgetArgsError("faltou o valor")
    *category_words, raw_amount = args
    try:
        limit_cents = parse_amount(raw_amount)
    except AmountError as exc:
        raise BudgetArgsError(f"o valor vem por último ({exc})") from exc
    # Aceita "/orcamento alimentação R$ 800": o "R$" solto não faz parte do nome.
    category_words = [w for w in category_words if normalize(w) != "r$"]
    category = " ".join(category_words).lstrip("#").strip()
    if not category:
        raise BudgetArgsError("faltou a categoria")
    return BudgetCommand("set", category, limit_cents)
