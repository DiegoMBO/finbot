"""Reconhece mensagens de pagamento de dívida e os argumentos de /pagar e /saldo.

    "paguei 450 do empréstimo nubank" -> PaymentIntent(45000, ["emprestimo", "nubank"])

Só os verbos de PAGAR contam ("paguei", "quitei"...). "gastei 50 no mercado"
nunca é tratado como pagamento de dívida.
"""

from __future__ import annotations

from dataclasses import dataclass

from finbot.money import AmountError, parse_amount
from finbot.parsing.message import AMOUNT_TOKEN, EDGE_PUNCT, STOPWORDS
from finbot.parsing.text import normalize

PAYMENT_VERBS = {"paguei", "pago", "paga", "pagamento", "quitei", "amortizei", "abati"}
# Palavras comuns em mensagens de pagamento que não identificam a dívida.
FILLER = STOPWORDS | {"parcela", "prestacao", "divida", "mes", "mensal", "valor", "ao", "pelo"}


class PaymentArgsError(ValueError):
    """Argumentos inválidos para /pagar ou /saldo."""


@dataclass(frozen=True)
class PaymentIntent:
    amount_cents: int
    words: list[str]  # palavras normalizadas que sobraram (para achar a dívida)


def parse_payment_intent(text: str) -> PaymentIntent | None:
    """None se a mensagem não parece pagamento (sem verbo de pagar ou sem valor)."""
    tokens = [t.strip(EDGE_PUNCT) for t in text.split()]
    tokens = [t for t in tokens if t]
    if not any(normalize(t) in PAYMENT_VERBS for t in tokens):
        return None

    amount: int | None = None
    words: list[str] = []
    for token in tokens:
        norm = normalize(token)
        if amount is None and AMOUNT_TOKEN.fullmatch(token):
            try:
                amount = parse_amount(token)
                continue
            except AmountError:
                return None
        if norm in PAYMENT_VERBS or norm in FILLER:
            continue
        words.append(norm.lstrip("#"))
    if amount is None:
        return None
    return PaymentIntent(amount, [w for w in words if w])


def parse_name_and_amount(args: list[str]) -> tuple[str, int]:
    """/pagar nubank 450  ->  ("nubank", 45000). O valor vem por último."""
    if len(args) < 2:
        raise PaymentArgsError("use: nome da dívida e depois o valor")
    *name_words, raw_amount = args
    try:
        amount = parse_amount(raw_amount)
    except AmountError as exc:
        raise PaymentArgsError(f"o valor vem por último ({exc})") from exc
    name_words = [w for w in name_words if normalize(w) != "r$"]
    if not name_words:
        raise PaymentArgsError("faltou o nome da dívida")
    return " ".join(name_words), amount


def parse_name_and_balance(args: list[str]) -> tuple[str, int]:
    """/saldo nubank 4.800,00 -> ("nubank", 480000). Aceita 0 (dívida quitada)."""
    if len(args) >= 2 and normalize(args[-1]) in {"0", "0,00", "0.00", "zero"}:
        name = " ".join(args[:-1])
        return name, 0
    return parse_name_and_amount(args)
