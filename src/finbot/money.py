"""Conversão entre o texto digitado e centavos (int), e formatação em reais.

Todo valor dentro do programa é um int em centavos. Só aqui ele vira texto.
"""

from __future__ import annotations


class AmountError(ValueError):
    """Texto que não é um valor em dinheiro válido."""


def parse_amount(text: str) -> int:
    """Converte "35", "12,50", "1.234,56", "12.5" ou "R$35" em centavos.

    Regras (formato brasileiro, com tolerância):
    - com vírgula: a vírgula separa os centavos e os pontos separam milhares;
    - sem vírgula: "1.500" (grupos de 3 dígitos após o ponto) é mil e quinhentos;
      "12.5" ou "12.50" (1 ou 2 dígitos após o ponto) é decimal.
    """
    raw = text.strip().lower().removeprefix("r$").strip().rstrip(".,")
    if not raw or not raw[0].isdigit():
        raise AmountError(f"{text!r} não é um valor")

    if "," in raw:
        if raw.count(",") > 1:
            raise AmountError(f"{text!r} tem mais de uma vírgula")
        int_part, dec_part = raw.split(",")
        int_part = int_part.replace(".", "")
    elif "." in raw:
        first, *rest = raw.split(".")
        if all(len(group) == 3 for group in rest):
            int_part, dec_part = first + "".join(rest), ""
        elif len(rest) == 1 and len(rest[0]) <= 2:
            int_part, dec_part = first, rest[0]
        else:
            raise AmountError(f"{text!r} não é um valor")
    else:
        int_part, dec_part = raw, ""

    if not int_part.isdigit() or (dec_part and not dec_part.isdigit()) or len(dec_part) > 2:
        raise AmountError(f"{text!r} não é um valor")

    cents = int(int_part) * 100 + int(dec_part.ljust(2, "0"))
    if cents == 0:
        raise AmountError("o valor precisa ser maior que zero")
    return cents


def format_percent(ppm: int) -> str:
    """Taxa em milionésimos -> texto: 19900 -> "1,99%", 35000 -> "3,50%", 4350250 -> "435,025%"."""
    whole, frac = divmod(ppm, 10_000)  # 10.000 ppm = 1%
    decimals = f"{frac:04d}".rstrip("0").ljust(2, "0")  # no mínimo 2 casas, no máximo 4
    return f"{whole},{decimals}%"


def format_brl(cents: int) -> str:
    """1234567 -> "R$ 12.345,67"."""
    sign = "-" if cents < 0 else ""
    reais, centavos = divmod(abs(cents), 100)
    # f"{12345:,}" dá "12,345"; trocamos a vírgula pelo ponto brasileiro.
    return f"{sign}R$ {reais:,}".replace(",", ".") + f",{centavos:02d}"
