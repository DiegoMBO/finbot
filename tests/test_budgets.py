"""Testes do /orcamento: argumentos, gravação, uso no mês e alertas."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from finbot import db
from finbot.bot.handlers import format_alert, format_budget_line, format_month_report
from finbot.parsing.budget import BudgetArgsError, BudgetCommand, parse_budget_args
from finbot.parsing.message import ParsedEntry
from finbot.services import budgets
from finbot.services import transactions as tx

SP = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 11, 26, 15, tzinfo=UTC)


@pytest.fixture
def conn():
    c = db.connect(db.MEMORY)
    yield c
    c.close()


def gasto(conn, cents, category="alimentação", when=NOW):
    return tx.add(conn, ParsedEntry("expense", cents, category, ""), when)


# --- argumentos -------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], BudgetCommand("list")),
        (["alimentação", "800"], BudgetCommand("set", "alimentação", 80000)),
        (["comida", "fora", "300,50"], BudgetCommand("set", "comida fora", 30050)),
        (["#lazer", "R$", "200"], BudgetCommand("set", "lazer", 20000)),
        (["remover", "lazer"], BudgetCommand("remove", "lazer")),
        (["Apagar", "comida", "fora"], BudgetCommand("remove", "comida fora")),
    ],
)
def test_args_validos(args, expected):
    assert parse_budget_args(args) == expected


@pytest.mark.parametrize(
    ("args", "msg"),
    [
        (["alimentação"], "valor"),
        (["800"], "valor"),
        (["remover"], "categoria"),
        (["lazer", "muito"], "valor"),
        (["R$", "800"], "categoria"),
    ],
)
def test_args_invalidos(args, msg):
    with pytest.raises(BudgetArgsError, match=msg):
        parse_budget_args(args)


# --- gravação ---------------------------------------------------------------


def test_sem_acento_usa_nome_da_categoria_conhecida(conn):
    b = budgets.set_budget(conn, "Alimentacao", 80000)
    assert b.category == "alimentação"


def test_categoria_nova_vem_de_transacao_existente(conn):
    gasto(conn, 1000, "família")
    assert budgets.set_budget(conn, "FAMILIA", 5000).category == "família"


def test_definir_de_novo_substitui(conn):
    budgets.set_budget(conn, "alimentação", 80000)
    budgets.set_budget(conn, "alimentacao", 90000)
    assert budgets.list_budgets(conn) == [budgets.Budget("alimentação", 90000)]


def test_remover(conn):
    budgets.set_budget(conn, "lazer", 20000)
    assert budgets.remove_budget(conn, "LAZER").category == "lazer"
    assert budgets.remove_budget(conn, "lazer") is None
    assert budgets.list_budgets(conn) == []


# --- uso no mês -------------------------------------------------------------


def test_month_statuses_soma_so_o_mes_e_ordena_pelo_mais_apertado(conn):
    budgets.set_budget(conn, "alimentação", 10000)
    budgets.set_budget(conn, "transporte", 10000)
    gasto(conn, 3000)
    gasto(conn, 9000, "transporte")
    gasto(conn, 5000, when=datetime(2026, 10, 15, tzinfo=UTC))  # outubro: não conta
    tx.add(conn, ParsedEntry("income", 99999, "alimentação", ""), NOW)  # entrada: não conta

    result = budgets.month_statuses(conn, NOW, SP)
    assert [(s.category, s.spent_cents, s.percent) for s in result] == [
        ("transporte", 9000, 90),
        ("alimentação", 3000, 30),
    ]


# --- alertas ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (0, 7999, None),
        (7000, 8000, 80),  # exatamente 80% conta
        (8000, 9000, None),  # já estava acima de 80%: não repete
        (7000, 10000, 100),  # pulou os dois: vale o 100
        (9000, 12000, 100),
        (10000, 11000, None),  # já estourado: não repete
    ],
)
def test_crossed_threshold(before, after, expected):
    assert budgets.crossed_threshold(before, after, 10000) == expected


def test_check_alert_dispara_ao_cruzar(conn):
    budgets.set_budget(conn, "alimentação", 10000)
    assert budgets.check_alert(conn, gasto(conn, 5000), SP) is None
    alert = budgets.check_alert(conn, gasto(conn, 3500), SP)
    assert (alert.threshold, alert.status.spent_cents) == (80, 8500)
    assert budgets.check_alert(conn, gasto(conn, 500), SP) is None
    alert = budgets.check_alert(conn, gasto(conn, 2000), SP)
    assert (alert.threshold, alert.status.spent_cents) == (100, 11000)


def test_check_alert_sem_orcamento_ou_entrada(conn):
    assert budgets.check_alert(conn, gasto(conn, 5000), SP) is None
    budgets.set_budget(conn, "salário", 100)
    entrada = tx.add(conn, ParsedEntry("income", 5000, "salário", ""), NOW)
    assert budgets.check_alert(conn, entrada, SP) is None


# --- textos -----------------------------------------------------------------


def test_textos():
    ok = budgets.BudgetStatus("lazer", 20000, 5000)
    quase = budgets.BudgetStatus("alimentação", 80000, 68000)
    estourou = budgets.BudgetStatus("transporte", 10000, 12550)
    assert format_budget_line(ok) == "• lazer: R$ 50,00 de R$ 200,00 (25%)"
    assert format_budget_line(quase).endswith("(85%) ⚠️")
    assert format_budget_line(estourou).endswith("(125%) 🚨")
    assert format_alert(budgets.BudgetAlert(80, quase)) == (
        "⚠️ Você já usou 85% do orçamento de alimentação: restam R$ 120,00 este mês."
    )
    assert format_alert(budgets.BudgetAlert(100, estourou)) == (
        "🚨 Orçamento de transporte estourado: R$ 125,50 de R$ 100,00 (passou R$ 25,50)."
    )


def test_mes_mostra_orcamentos():
    summary = tx.Summary(1000, 0, {"lazer": 1000})
    text = format_month_report(
        "novembro/2026", summary, 1, [budgets.BudgetStatus("lazer", 2000, 1000)]
    )
    assert "Orçamentos:\n• lazer: R$ 10,00 de R$ 20,00 (50%)" in text
