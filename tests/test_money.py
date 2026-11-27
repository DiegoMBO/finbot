"""Testes de conversão e formatação de valores."""

import pytest

from finbot.money import AmountError, format_brl, parse_amount


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("35", 3500),
        ("12,50", 1250),
        ("12,5", 1250),
        ("12.50", 1250),
        ("12.5", 1250),
        ("1.500", 150000),
        ("1.234,56", 123456),
        ("1.234.567", 123456700),
        ("R$35", 3500),
        ("r$ 9,90", 990),
        ("35,", 3500),
        ("0,01", 1),
    ],
)
def test_valores_validos(text, cents):
    assert parse_amount(text) == cents


@pytest.mark.parametrize("text", ["", "abc", "0", "0,00", "1,2,3", "12,345", "1.23.4", "12.3456"])
def test_valores_invalidos(text):
    with pytest.raises(AmountError):
        parse_amount(text)


@pytest.mark.parametrize(
    ("cents", "text"),
    [
        (0, "R$ 0,00"),
        (5, "R$ 0,05"),
        (1250, "R$ 12,50"),
        (123456789, "R$ 1.234.567,89"),
        (-1250, "-R$ 12,50"),
    ],
)
def test_format_brl(cents, text):
    assert format_brl(cents) == text
