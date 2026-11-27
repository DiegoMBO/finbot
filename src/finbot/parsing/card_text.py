"""Mensagens de texto livre que citam um cartão.

    "gastei 80 no mercado no nubank"         -> compra à vista
    "comprei tv 3000 em 10x no nubank"       -> compra em 10 parcelas
    "paguei 1500 da fatura do nubank"        -> pagamento da fatura
    "paguei 50 no mercado no nubank"         -> ambíguo (compra ou pagamento?)

Quem chama já sabe QUAL cartão foi citado; aqui só limpamos o texto e lemos
valor, parcelas, categoria, descrição e a intenção.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from finbot.parsing.message import EDGE_PUNCT, ParsedEntry, ParseError, parse_message
from finbot.parsing.payment import PAYMENT_VERBS
from finbot.parsing.text import normalize

CARD_WORDS = {"cartao", "credito", "fatura", "pelo", "pela"}
# "10x", "10 x", "em 10 vezes", "10 parcelas"; também "x10"
_INSTALLMENTS = re.compile(r"^(?:x(\d{1,2})|(\d{1,2})x)$")
_INSTALLMENT_WORDS = {"x", "vezes", "parcelas", "parcela"}
_CASH_WORDS = {"vista", "avista"}

Intent = Literal["compra", "pagamento", "ambiguo"]


@dataclass(frozen=True)
class CardMessage:
    intent: Intent
    entry: ParsedEntry  # valor TOTAL, categoria e descrição (sem o nome do cartão)
    installments: int


def _installments(tokens: list[str]) -> tuple[int, list[str]]:
    """Acha "10x" / "10 vezes" e devolve (parcelas, tokens sem isso)."""
    kept: list[str] = []
    found = 1
    skip_next = False
    for i, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        norm = normalize(token)
        match = _INSTALLMENTS.match(norm)
        if match:
            found = int(match.group(1) or match.group(2))
            continue
        if (
            norm.isdigit()
            and i + 1 < len(tokens)
            and normalize(tokens[i + 1]) in _INSTALLMENT_WORDS
        ):
            # "10 vezes" -- mas só se não for o primeiro número (o valor)
            if any(any(ch.isdigit() for ch in t) for t in kept):
                found = int(norm)
                skip_next = True
                continue
        if norm in _CASH_WORDS or (
            norm == "a" and i + 1 < len(tokens) and normalize(tokens[i + 1]) in _CASH_WORDS
        ):
            continue
        kept.append(token)
    if kept and normalize(kept[-1]) == "em":  # "... em" que sobrou de "em 10x"
        kept.pop()
    return found, kept


def parse_card_message(text: str, card_words: set[str]) -> CardMessage | None:
    """None se não der para ler como compra/pagamento (sem valor, ou é entrada)."""
    tokens = [t.strip(EDGE_PUNCT) for t in text.split()]
    tokens = [t for t in tokens if t]
    norms = [normalize(t) for t in tokens]
    is_payment = any(n in PAYMENT_VERBS for n in norms)
    mentions_invoice = "fatura" in norms

    cleaned = [t for t, n in zip(tokens, norms, strict=True) if n not in card_words | CARD_WORDS]
    installments, cleaned = _installments(cleaned)
    if not 1 <= installments <= 48:
        return None
    try:
        entry = parse_message(" ".join(cleaned))
    except ParseError:
        return None
    if entry.kind != "expense":
        return None

    if not is_payment:
        intent: Intent = "compra"
    elif mentions_invoice or not entry.description:
        intent = "pagamento"  # "paguei 1500 da fatura do nubank" / "paguei 1500 nubank"
    else:
        intent = "ambiguo"  # "paguei 50 no mercado no nubank"
    return CardMessage(intent, entry, installments)
