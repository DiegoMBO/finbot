"""Leitura das respostas do cadastro de dívidas (um campo por vez).

Cada função recebe o texto digitado e devolve o valor já convertido, ou lança
`FieldError` com uma explicação curta que o bot mostra antes de perguntar de novo.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from finbot.money import AmountError, parse_amount
from finbot.parsing.text import normalize
from finbot.services.finance import annual_to_monthly_ppm

SKIP_WORDS = {"pular", "pula", "pulo", "nao", "nao sei", "-", "nenhum", "nenhuma", "sem"}
MAX_NAME = 40
MAX_MONTHLY_RATE_PPM = 1_000_000  # 100% ao mês: acima disso é quase certo erro de digitação

# Palavras que você pode digitar no lugar de tocar no botão do tipo.
_KIND_WORDS = {
    "emprestimo": "emprestimo",
    "cartao": "cartao",
    "cartao de credito": "cartao",
    "fatura": "cartao",
    "financiamento": "financiamento",
    "cheque especial": "cheque_especial",
    "cheque": "cheque_especial",
    "parcelamento": "parcelamento",
    "parcelamento de compra": "parcelamento",
    "parcelado": "parcelamento",
    "pessoa": "pessoa",
    "divida com pessoa": "pessoa",
}

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
# "30% a.a.", "30% aa", "30% ao ano", "30 anual"; "a.m." (ao mês) não casa.
_ANNUAL = re.compile(r"\ba\.?\s?a\b|ao ano|anual|por ano")


class FieldError(ValueError):
    """Resposta inválida para o campo perguntado."""


def is_skip(text: str) -> bool:
    return normalize(text.strip()) in SKIP_WORDS


def parse_name(text: str) -> str:
    name = " ".join(text.split())  # tira espaços repetidos
    if not name:
        raise FieldError("o nome não pode ficar vazio")
    if len(name) > MAX_NAME:
        raise FieldError(f"use no máximo {MAX_NAME} caracteres")
    if name.startswith("/"):
        raise FieldError("o nome não pode começar com /")
    return name


def parse_kind(text: str) -> str:
    kind = _KIND_WORDS.get(normalize(" ".join(text.split())))
    if kind is None:
        raise FieldError("toque em um dos botões de tipo")
    if kind == "cartao":
        raise FieldError("cartão de crédito agora tem cadastro próprio: use /novocartao")
    return kind


def parse_text(text: str) -> str:
    value = " ".join(text.split())
    if not value:
        raise FieldError("a resposta ficou vazia")
    return value[:MAX_NAME]


def parse_money(text: str) -> int:
    try:
        return parse_amount(text)
    except AmountError as exc:
        raise FieldError(f"valor inválido ({exc}); exemplo: 4.200,00") from exc


def parse_money_or_zero(text: str) -> int:
    """Como parse_money, mas aceita 0 (ex.: limite utilizado zerado)."""
    if normalize(text.strip()) in {"0", "0,00", "0.00", "zero", "nada"}:
        return 0
    return parse_money(text)


def parse_money_list(text: str) -> tuple[int, ...]:
    """ "600 400 250" ou "600; 400; 250" -> (60000, 40000, 25000)."""
    parts = [p for p in re.split(r"[;\s]+", text.strip()) if p and normalize(p) != "r$"]
    if not parts:
        raise FieldError("escreva os valores separados por espaço, ex.: 600 400 250")
    if len(parts) > 12:
        raise FieldError("informe no máximo 12 faturas")
    return tuple(parse_money(p) for p in parts)


def parse_rate(text: str) -> int:
    """Taxa em % -> ppm. "2,5" / "2,5%" / "2,5% a.m." = mensal; "30% a.a." = anual.

    Aqui "1.99" é decimal (não milhar): taxa nunca chega a mil por cento.
    """
    norm = normalize(text)
    match = _NUMBER.search(norm)
    if not match:
        raise FieldError("escreva só o número, ex.: 2,5 (para 2,5% ao mês)")
    try:
        percent = Decimal(match.group().replace(",", "."))
    except InvalidOperation as exc:  # pragma: no cover (a regex já garante o formato)
        raise FieldError("taxa inválida") from exc
    # 1% = 10.000 ppm; arredonda a 4 casas do percentual.
    ppm = int((percent * 10_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if _ANNUAL.search(norm):
        ppm = annual_to_monthly_ppm(ppm)
    if ppm > MAX_MONTHLY_RATE_PPM:
        raise FieldError("taxa acima de 100% ao mês; confira o valor")
    return ppm


def parse_installments(text: str) -> int:
    value = text.strip()
    if not value.isdigit() or not 1 <= int(value) <= 600:
        raise FieldError("escreva um número inteiro de parcelas, de 1 a 600")
    return int(value)


def parse_day(text: str) -> int:
    value = text.strip()
    if not value.isdigit() or not 1 <= int(value) <= 31:
        raise FieldError("escreva um dia de 1 a 31")
    return int(value)


def parse_date(text: str, today: date) -> date:
    """ "hoje", "dd/mm/aaaa", "dd/mm/aa" ou "mm/aaaa" (considera o dia 1)."""
    value = normalize(text.strip())
    if value == "hoje":
        return today
    parts = re.split(r"[/.-]", value)
    try:
        if len(parts) == 3:
            day, month, year = (int(p) for p in parts)
        elif len(parts) == 2:
            day, (month, year) = 1, (int(p) for p in parts)
        else:
            raise ValueError
        if year < 100:
            year += 2000
        result = date(year, month, day)
    except ValueError as exc:
        raise FieldError("use dd/mm/aaaa ou mm/aaaa, ex.: 15/03/2025") from exc
    if result > today:
        raise FieldError("a data de início não pode estar no futuro")
    return result
