"""Textos do módulo de salário: horas extras e respostas do cadastro.

"fiz 10 horas extras"                  -> 600 min, adicional normal, hoje
"fiz 2h30 extras" / "fiz 2:30 de extra" -> 150 min
"fiz 4 horas extras no domingo"         -> adicional de domingo/feriado
"fiz 10 horas extras em novembro"       -> lançado em novembro
"fiz 3 horas extras ontem" / "dia 25"   -> nessa data
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from finbot.money import AmountError, parse_amount
from finbot.parsing.debt import FieldError
from finbot.parsing.period import MONTH_NAMES
from finbot.parsing.text import normalize

_HOURS = re.compile(
    r"(?<![\d,.])(\d{1,3})(?:[.,](\d{1,2}))?\s*(?:h|hr|hrs|hora|horas)\b(?:\s*e?\s*(\d{1,2})\s*(?:m|min|minutos?)\b)?"
)
_HHMM = re.compile(r"(?<![\d,.])(\d{1,3})(?:h|:)(\d{2})\b")
_DAY = re.compile(r"\bdia\s+(\d{1,2})(?:/(\d{1,2}))?\b")
_MONTHS = {normalize(name): i for i, name in enumerate(MONTH_NAMES, 1)}


@dataclass(frozen=True)
class OvertimeIntent:
    minutes: int
    holiday: bool  # domingo/feriado -> adicional de feriado
    worked_on: date


def _minutes(norm: str) -> int | None:
    match = _HHMM.search(norm)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    match = _HOURS.search(norm)
    if not match:
        return None
    hours = int(match.group(1))
    minutes = 0
    if match.group(2):  # 2,5 h -> 2h30 ; 1,25 h -> 1h15
        frac = match.group(2).ljust(2, "0")
        minutes = round(int(frac) * 60 / 100)
    if match.group(3):
        minutes += int(match.group(3))
    return hours * 60 + minutes


def _worked_on(norm: str, today: date) -> date:
    if "anteontem" in norm:
        return today - timedelta(days=2)
    if "ontem" in norm:
        return today - timedelta(days=1)
    match = _DAY.search(norm)
    if match:
        day, month = int(match.group(1)), int(match.group(2) or today.month)
        year = today.year if (month, day) <= (today.month, today.day) else today.year - 1
        try:
            return date(year, month, day)
        except ValueError:
            return today
    for word in norm.split():
        month = _MONTHS.get(word)
        if month:
            year = today.year if month <= today.month else today.year - 1
            return today if (year, month) == (today.year, today.month) else date(year, month, 1)
    return today


def parse_overtime(text: str, today: date) -> OvertimeIntent | None:
    """None se a mensagem não é registro de hora extra."""
    norm = normalize(text)
    if "extra" not in norm:
        return None
    minutes = _minutes(norm)
    if not minutes or minutes > 24 * 60 * 31:
        return None
    holiday = bool(re.search(r"\b(domingo|feriado)\b", norm))
    return OvertimeIntent(minutes, holiday, _worked_on(norm, today))


def format_minutes(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    return f"{hours}h{rest:02d}" if rest else f"{hours}h"


# --- respostas do cadastro --------------------------------------------------------


def parse_payday(text: str) -> str:
    """Quando cai o pagamento com os descontos:
    "5º dia útil" -> '5du' (mês seguinte)
    "dia 10" / "10" -> 'dia:10' (dia 10 do mês seguinte)
    "dia 30 do mesmo mês" / "30 do próprio mês" -> 'mes:30' (no próprio mês trabalhado)
    """
    norm = normalize(text)
    if "util" in norm or "quinto" in norm or re.search(r"\b5\s*(o|º|°)\b", norm):
        return "5du"
    match = re.search(r"\d{1,2}", norm)
    if match and 1 <= int(match.group()) <= 31:
        prefix = "mes" if re.search(r"\b(mesmo|proprio)\b", norm) else "dia"
        return f"{prefix}:{int(match.group())}"
    raise FieldError('responda "5º dia útil", "dia 10" (do mês seguinte) ou "dia 30 do mesmo mês"')


def parse_advance(text: str) -> tuple[int, int]:
    """ "40% dia 20" -> (40, 20)."""
    norm = normalize(text)
    pct = re.search(r"(\d{1,2})\s*%", norm)
    day = re.search(r"dia\s*(\d{1,2})", norm)
    if not pct or not day or not 1 <= int(pct.group(1)) <= 99 or not 1 <= int(day.group(1)) <= 31:
        raise FieldError("use, por exemplo: 40% dia 20")
    return int(pct.group(1)), int(day.group(1))


def parse_int(text: str, low: int, high: int, what: str) -> int:
    value = text.strip().rstrip("%").strip()
    if not value.isdigit() or not low <= int(value) <= high:
        raise FieldError(f"{what}: um número de {low} a {high}")
    return int(value)


@dataclass(frozen=True)
class DeductionSpec:
    name: str
    percent_bp: int | None
    fixed_cents: int | None


def parse_deductions(text: str) -> list[DeductionSpec]:
    """ "vale-transporte 6%, plano de saúde 150" -> [VT 6% ; plano R$ 150]."""
    found = []
    # Separa em ";" e em vírgula seguida de texto (a de "150,50" é dos centavos).
    for part in (p.strip() for p in re.split(r";|\n|,(?=\s*[^\d\s])", text) if p.strip()):
        match = re.fullmatch(r"(.+?)\s+(r\$\s*)?([\d.,]+)\s*(%)?", part, re.IGNORECASE)
        if not match:
            raise FieldError(f"não entendi {part!r}; use nome e valor, ex.: plano de saúde 150")
        name, raw, is_pct = match.group(1).strip(), match.group(3), match.group(4)
        try:
            if is_pct:
                value = round(float(raw.replace(",", ".")) * 100)
                if not 1 <= value <= 10_000:
                    raise ValueError
                found.append(DeductionSpec(name, value, None))
            else:
                found.append(DeductionSpec(name, None, parse_amount(raw)))
        except (ValueError, AmountError) as exc:
            raise FieldError(f"valor inválido em {part!r}") from exc
    return found
