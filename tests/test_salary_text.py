"""Leitura dos textos do salário: horas extras e respostas do cadastro."""

from datetime import date

import pytest

from finbot.parsing.debt import FieldError
from finbot.parsing.salary_text import (
    DeductionSpec,
    format_minutes,
    parse_advance,
    parse_deductions,
    parse_overtime,
    parse_payday,
)

TODAY = date(2026, 11, 27)


@pytest.mark.parametrize(
    ("text", "minutes", "holiday", "worked_on"),
    [
        ("fiz 10 horas extras", 600, False, TODAY),
        ("fiz 2h30 extras", 150, False, TODAY),
        ("fiz 2:30 de hora extra", 150, False, TODAY),
        ("fiz 1,5 hora extra", 90, False, TODAY),
        ("fiz 3 horas e 20 minutos extras", 200, False, TODAY),
        ("fiz 4 horas extras no domingo", 240, True, TODAY),
        ("fiz 8h extras no feriado", 480, True, TODAY),
        ("fiz 3 horas extras ontem", 180, False, date(2026, 11, 26)),
        ("fiz 2 horas extras dia 20", 120, False, date(2026, 11, 20)),
        ("fiz 10 horas extras em outubro", 600, False, date(2026, 10, 1)),
        ("fiz 5 horas extras em novembro", 300, False, TODAY),
        ("fiz 6 horas extras em dezembro", 360, False, date(2025, 12, 1)),  # dezembro passado
    ],
)
def test_horas_extras(text, minutes, holiday, worked_on):
    r = parse_overtime(text, TODAY)
    assert (r.minutes, r.holiday, r.worked_on) == (minutes, holiday, worked_on)


@pytest.mark.parametrize(
    "text", ["gastei 50 no extra", "fiz 10 horas de academia", "comprei 2 cafés extra", "extra"]
)
def test_nao_e_hora_extra(text):
    assert parse_overtime(text, TODAY) is None


def test_formato_das_horas():
    assert (format_minutes(600), format_minutes(150)) == ("10h", "2h30")


@pytest.mark.parametrize(
    ("text", "rule"),
    [("5º dia útil", "5du"), ("quinto dia util", "5du"), ("dia 10", "dia:10"), ("30", "dia:30")],
)
def test_dia_do_pagamento(text, rule):
    assert parse_payday(text) == rule


def test_vale_e_descontos():
    assert parse_advance("40% dia 20") == (40, 20)
    with pytest.raises(FieldError):
        parse_advance("dia 20")
    assert parse_deductions("vale-transporte 6%, plano de saúde R$ 150,50") == [
        DeductionSpec("vale-transporte", 600, None),
        DeductionSpec("plano de saúde", None, 15_050),
    ]
    with pytest.raises(FieldError):
        parse_deductions("plano")
