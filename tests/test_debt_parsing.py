"""Testes da leitura das respostas do cadastro de dívidas."""

from datetime import date

import pytest

from finbot.money import format_percent
from finbot.parsing import debt as p
from finbot.services.finance import annual_to_monthly_ppm

TODAY = date(2026, 11, 26)


@pytest.mark.parametrize(
    ("text", "ppm"),
    [
        ("2", 20_000),
        ("2,5", 25_000),
        ("2,5%", 25_000),
        ("1.99", 19_900),  # ponto aqui é decimal: taxa nunca chega a mil %
        ("1,99% a.m.", 19_900),
        ("0", 0),
        ("0,0001", 1),
        ("15% ao mês", 150_000),
    ],
)
def test_taxa_mensal(text, ppm):
    assert p.parse_rate(text) == ppm


@pytest.mark.parametrize("text", ["30% a.a.", "30 aa", "30% ao ano", "30 anual"])
def test_taxa_anual_vira_mensal(text):
    # (1,30)^(1/12) − 1 = e^(ln 1,3 / 12) − 1 = e^(0,262364 / 12) − 1 = e^0,0218637 − 1 ≈ 2,2104%
    ppm = p.parse_rate(text)
    assert ppm == annual_to_monthly_ppm(300_000)
    assert 22_100 <= ppm <= 22_110


@pytest.mark.parametrize("text", ["muito", "", "150"])
def test_taxa_invalida(text):
    with pytest.raises(p.FieldError):
        p.parse_rate(text)


@pytest.mark.parametrize(
    ("ppm", "text"),
    [(19_900, "1,99%"), (35_000, "3,50%"), (4_350_250, "435,025%"), (0, "0,00%"), (5, "0,0005%")],
)
def test_format_percent(ppm, text):
    assert format_percent(ppm) == text


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Empréstimo", "emprestimo"),
        ("CHEQUE  especial", "cheque_especial"),
        ("parcelado", "parcelamento"),
        ("pessoa", "pessoa"),
    ],
)
def test_tipo_digitado(text, kind):
    assert p.parse_kind(text) == kind


def test_cartao_manda_para_o_cadastro_de_cartao():
    with pytest.raises(p.FieldError, match="/novocartao"):
        p.parse_kind("cartão de crédito")


def test_tipo_desconhecido():
    with pytest.raises(p.FieldError):
        p.parse_kind("agiota")


def test_nome():
    assert p.parse_name("  Empréstimo   Nubank ") == "Empréstimo Nubank"
    for bad in ["", "   ", "/comando", "x" * 41]:
        with pytest.raises(p.FieldError):
            p.parse_name(bad)


@pytest.mark.parametrize(
    ("text", "skip"), [("pular", True), ("Não", True), ("-", True), ("nubank", False)]
)
def test_pular(text, skip):
    assert p.is_skip(text) is skip


def test_dia_e_parcelas():
    assert p.parse_day("31") == 31
    assert p.parse_installments("24") == 24
    for bad in ["0", "32", "dez", "1,5"]:
        with pytest.raises(p.FieldError):
            p.parse_day(bad)
    for bad in ["0", "601", "dez"]:
        with pytest.raises(p.FieldError):
            p.parse_installments(bad)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hoje", TODAY),
        ("15/03/2025", date(2025, 3, 15)),
        ("15/03/25", date(2025, 3, 15)),
        ("03/2025", date(2025, 3, 1)),
        ("3-2025", date(2025, 3, 1)),
    ],
)
def test_data(text, expected):
    assert p.parse_date(text, TODAY) == expected


@pytest.mark.parametrize("text", ["31/02/2025", "ontem", "01/01/2030", "2025"])
def test_data_invalida(text):
    with pytest.raises(p.FieldError):
        p.parse_date(text, TODAY)
