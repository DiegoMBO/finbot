"""Argumentos do /fatura.

/fatura nubank                       -> mostra a fatura atual
/fatura nubank 1.850,40              -> registra a fatura (vencimento = dia cadastrado)
/fatura nubank 1.850,40 277,56       -> com o pagamento mínimo
/fatura nubank 1.850,40 vence 10/12  -> informando o vencimento
/fatura cartão nubank 1850,40 mínimo 277,56 10/12/2026
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from finbot.money import AmountError, parse_amount
from finbot.parsing.message import AMOUNT_TOKEN
from finbot.parsing.text import normalize

MARKERS = {"vence", "vencimento", "venc", "minimo", "min", "total", "r$", "em", "dia"}
_DATE = re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?")


class CardArgsError(ValueError):
    """Argumentos inválidos para /fatura."""


@dataclass(frozen=True)
class StatementArgs:
    name: str
    total_cents: int | None  # None = só mostrar a fatura atual
    minimum_cents: int | None
    due_date: date | None


def resolve_day_month(day: int, month: int, today: date) -> date:
    """ "10/12" sem ano: escolhe o ano que deixa a data mais perto de hoje.

    Em 26/11/2026: "10/12" -> 10/12/2026 e "05/01" -> 05/01/2027.
    """
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        raise CardArgsError(f"data inválida: {day:02d}/{month:02d}")
    return min(candidates, key=lambda d: abs(d - today))


def _parse_date(token: str, today: date) -> date:
    match = _DATE.fullmatch(token)
    day, month, raw_year = int(match.group(1)), int(match.group(2)), match.group(3)
    if raw_year is None:
        return resolve_day_month(day, month, today)
    year = int(raw_year) + (2000 if len(raw_year) == 2 else 0)
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise CardArgsError(f"data inválida: {token}") from exc


def parse_statement_args(args: list[str], today: date) -> StatementArgs:
    name_words: list[str] = []
    amounts: list[int] = []
    due: date | None = None
    for token in args:
        if _DATE.fullmatch(token):
            due = _parse_date(token, today)
        elif AMOUNT_TOKEN.fullmatch(token):
            try:
                amounts.append(parse_amount(token))
            except AmountError as exc:
                raise CardArgsError(f"valor inválido ({exc})") from exc
        elif normalize(token) not in MARKERS:
            name_words.append(token)
    if not name_words:
        raise CardArgsError("faltou o nome do cartão")
    if len(amounts) > 2:
        raise CardArgsError("use no máximo dois valores: total e mínimo")
    if due is not None and not (today - timedelta(days=60) <= due <= today + timedelta(days=60)):
        raise CardArgsError("o vencimento precisa estar a no máximo 60 dias de hoje")
    total = amounts[0] if amounts else None
    minimum = amounts[1] if len(amounts) > 1 else None
    if minimum is not None and minimum > total:
        raise CardArgsError("o mínimo não pode ser maior que o total")
    return StatementArgs(" ".join(name_words), total, minimum, due)
