"""Transforma "gastei 35 no mercado" em um `ParsedEntry`.

Formato aceito (bem tolerante, a ordem das palavras não importa):
- um valor: 35 | 12,50 | 1.234,56 | R$35
- palavras que indicam ENTRADA: recebi, ganhei, entrou, entrada...
  (sem elas, é um GASTO);
- o resto vira a descrição, sem palavras vazias como "no", "de", "com";
- opcional: #categoria força a categoria ("35 presente #família").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from finbot.money import AmountError, parse_amount
from finbot.parsing.categories import guess_category
from finbot.parsing.text import normalize

Kind = Literal["expense", "income"]

INCOME_WORDS = {"recebi", "ganhei", "entrou", "entrada", "receita", "recebido"}
EXPENSE_WORDS = {"gastei", "paguei", "comprei", "gasto", "saida", "pago"}
STOPWORDS = {
    "no",
    "na",
    "nos",
    "nas",
    "de",
    "do",
    "da",
    "dos",
    "das",
    "em",
    "com",
    "pro",
    "pra",
    "para",
    "o",
    "a",
    "os",
    "as",
    "um",
    "uma",
    "reais",
    "real",
    "r$",
}

# Um token que começa com dígito (ou "R$" + dígito) é candidato a valor.
AMOUNT_TOKEN = re.compile(r"(?:r\$)?\d[\d.,]*", re.IGNORECASE)
# Pontuação solta nas pontas das palavras: "mercado," -> "mercado".
EDGE_PUNCT = "!?;:()\"'"


class ParseError(ValueError):
    """Mensagem que não dá para entender como gasto/entrada."""


@dataclass(frozen=True)
class ParsedEntry:
    kind: Kind
    amount_cents: int
    category: str
    description: str


def parse_message(text: str) -> ParsedEntry:
    tokens = [t.strip(EDGE_PUNCT) for t in text.split()]
    tokens = [t for t in tokens if t]

    amount_cents: int | None = None
    kind: Kind = "expense"
    explicit_category: str | None = None
    description_words: list[str] = []

    for token in tokens:
        norm = normalize(token)
        # Só o PRIMEIRO número é o valor; os outros fazem parte da descrição
        # (ex.: "15 no 99" é R$ 15 de transporte por app).
        if amount_cents is None and AMOUNT_TOKEN.fullmatch(token):
            try:
                amount_cents = parse_amount(token)
                continue
            except AmountError as exc:
                raise ParseError(str(exc)) from exc
        if token.startswith("#") and len(token) > 1:
            explicit_category = token[1:].lower()
        elif norm in INCOME_WORDS:
            kind = "income"
        elif norm in EXPENSE_WORDS or norm in STOPWORDS:
            pass  # não acrescenta nada à descrição
        else:
            description_words.append(token)

    if amount_cents is None:
        raise ParseError("não encontrei o valor na mensagem")

    category = explicit_category or guess_category(description_words, kind)
    return ParsedEntry(
        kind=kind,
        amount_cents=amount_cents,
        category=category,
        description=" ".join(description_words),
    )
