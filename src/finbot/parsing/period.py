"""Interpretação do argumento opcional do /mes.

/mes          -> mês atual
/mes 10       -> outubro do ano atual
/mes 10/2025  -> outubro de 2025 (também aceita 10-2025 e 10/25)
"""

from __future__ import annotations

import re
from datetime import date

MONTH_NAMES = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)

_MONTH_ARG = re.compile(r"(\d{1,2})(?:[/-](\d{2}|\d{4}))?")


class PeriodError(ValueError):
    """Argumento de mês inválido."""


def parse_month(arg: str | None, today: date) -> tuple[int, int]:
    """Devolve (ano, mês). `today` vem de fora para os testes serem previsíveis."""
    if arg is None or not arg.strip():
        return today.year, today.month

    match = _MONTH_ARG.fullmatch(arg.strip())
    if not match:
        raise PeriodError("use /mes, /mes 10 ou /mes 10/2025")

    month = int(match.group(1))
    if not 1 <= month <= 12:
        raise PeriodError(f"mês {month} não existe")

    raw_year = match.group(2)
    if raw_year is None:
        year = today.year
    elif len(raw_year) == 2:
        year = 2000 + int(raw_year)  # "25" -> 2025
    else:
        year = int(raw_year)
    return year, month


def month_label(year: int, month: int) -> str:
    """(2026, 11) -> "novembro/2026". Nomes fixos: o locale do Windows não é confiável."""
    return f"{MONTH_NAMES[month - 1]}/{year}"
