"""Testes do /mes: argumento do mês, limites do mês e texto do relatório."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from finbot import db
from finbot.bot.handlers import format_month_report
from finbot.parsing.message import ParsedEntry
from finbot.parsing.period import PeriodError, month_label, parse_month
from finbot.services import transactions as tx

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2026, 11, 26)


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        (None, (2026, 11)),
        ("", (2026, 11)),
        ("10", (2026, 10)),
        ("3/2025", (2025, 3)),
        ("03-2025", (2025, 3)),
        ("12/25", (2025, 12)),
    ],
)
def test_parse_month_valido(arg, expected):
    assert parse_month(arg, TODAY) == expected


@pytest.mark.parametrize("arg", ["13", "0", "outubro", "10/2", "10/2025/1"])
def test_parse_month_invalido(arg):
    with pytest.raises(PeriodError):
        parse_month(arg, TODAY)


def test_month_label():
    assert month_label(2026, 3) == "março/2026"


def test_month_bounds_virada_de_ano():
    start, end = tx.month_bounds(2026, 12, SP)
    assert (start.year, start.month, end.year, end.month) == (2026, 12, 2027, 1)


def test_mes_respeita_fuso():
    conn = db.connect(db.MEMORY)
    entry = ParsedEntry("expense", 1000, "outros", "")
    # 31/10 às 22:00 em São Paulo = 01/11 01:00 UTC: tem que contar em OUTUBRO.
    tx.add(conn, entry, datetime(2026, 11, 1, 1, 0, tzinfo=UTC))
    # 01/11 às 00:30 em São Paulo = 01/11 03:30 UTC: novembro.
    tx.add(conn, entry, datetime(2026, 11, 1, 3, 30, tzinfo=UTC))

    assert len(tx.list_between(conn, *tx.month_bounds(2026, 10, SP))) == 1
    assert len(tx.list_between(conn, *tx.month_bounds(2026, 11, SP))) == 1
    conn.close()


def test_format_month_report():
    summary = tx.Summary(
        expense_cents=4750,
        income_cents=300000,
        expenses_by_category={"alimentação": 3500, "transporte": 1250},
    )
    text = format_month_report("novembro/2026", summary, count=3)
    assert text == (
        "📊 Resumo de novembro/2026\n"
        "\n"
        "Entradas: R$ 3.000,00\n"
        "Gastos: R$ 47,50\n"
        "Saldo: R$ 2.952,50\n"
        "\n"
        "Gastos por categoria:\n"
        "• alimentação: R$ 35,00 (74%)\n"
        "• transporte: R$ 12,50 (26%)\n"
        "\n"
        "3 registro(s) no mês."
    )


def test_format_month_report_so_entradas_nao_divide_por_zero():
    summary = tx.Summary(expense_cents=0, income_cents=5000, expenses_by_category={})
    text = format_month_report("novembro/2026", summary, count=1)
    assert "Gastos por categoria" not in text
    assert "Saldo: R$ 50,00" in text
