"""Cartões na estratégia e no /saude: faturas como pagamento obrigatório,
rotativo como dívida, cenário /semcartao e compras de antes do bot."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from fakes import Bot

from finbot import db
from finbot.bot import strategy
from finbot.bot.health import format_indicators
from finbot.config import HealthSettings
from finbot.services import credit_cards as cc
from finbot.services import debts, health, salary

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2026, 11, 27)
NOW = datetime(2026, 11, 27, 15, tzinfo=UTC)


@pytest.fixture
def conn():
    c = db.connect(db.MEMORY)
    # Nubank: usado 9.594,54; faturas dez 2.092,84, jan 1.814,86 (o resto, 5.686,84,
    # sem mês definido); rotativo 16,1%.
    cc.create(
        c,
        cc.NewCard("Nubank", 3, 10, 960_000, 959_454, 209_284, (181_486,), 161_000),
        TODAY,
        NOW,
    )
    debts.create(
        c,
        debts.NewDebt("santander", "cheque_especial", 128_640, rate_ppm=80_000),
        NOW,
    )
    debts.create(
        c,
        debts.NewDebt("clt-nubank", "emprestimo", 237_900, payment_cents=79_321, due_day=10),
        NOW,
    )
    yield c
    c.close()


def card(conn):
    return cc.list_cards(conn)[0]


def with_salary(conn):
    """O salário real: 4.500 bruto, 40% no dia 15, o resto no dia 30 do próprio mês."""
    salary.save_employment(
        conn, date(2026, 7, 6), salary.parse_schedule("30/06, 30/11, 18/12"), NOW
    )
    salary.add_profile(
        conn, date(2026, 7, 6), NOW, gross_cents=450_000, weekly_hours=40, overtime_pct=50,
        payday_rule="mes:30", advance_pct=40, advance_day=15, overtime_lag_months=2,
    )  # fmt: skip
    clt = next(d for d in debts.list_active(conn) if d.name == "clt-nubank")
    salary.add_deduction(conn, "consignado clt-nubank", NOW, debt_id=clt.id)


def test_estrategia_com_a_sobra_real_feita_a_mao(conn):
    with_salary(conn)
    inputs = strategy.strategy_inputs(debts.list_active(conn))
    # Resto de nov: 13º 1ª 937,50 + salário nov 1.475,28 (já sem consignado) = 2.412,78
    # Dez: vale 1.800 + 13º 2ª 1.134,31 + salário 2.268,49 = 5.202,80
    #      − fatura 2.092,84 − parcelas 793,21 = +2.316,75; com o de nov = 4.729,53
    plan = strategy.cash_plan(conn, inputs, TODAY, reserve_cents=0)
    assert (plan[0].key, plan[0].income_cents, plan[0].extra_cents) == ("2026-12", 520_280, 472_953)
    # 4.729,53 de extra em dez quita o santander (1.389,31 com juros) e o clt-nubank
    text = strategy.format_cash_strategy(inputs, plan, 0, TODAY)
    assert "santander (8,00% a.m.): quitada em dez/2026" in text
    assert "Reserva zero supõe" in text

    # Reservando 3.000/mês para viver: dez sobra 1.729,53;
    # jan: 1.800 + 2.268,49 − fatura 1.814,86 − 793,21 − 3.000 = −1.539,58 -> falta
    plan = strategy.cash_plan(conn, inputs, TODAY, reserve_cents=300_000)
    assert [(m.extra_cents, m.deficit_cents) for m in plan[:2]] == [(172_953, 0), (0, 153_958)]
    text = strategy.format_cash_strategy(inputs, plan, 300_000, TODAY)
    assert "🚨 Falta dinheiro (acumulado) em: jan/2027 (−R$ 1.539,58)" in text


def test_estrategia_sobra_pelo_bot(conn):
    bot = Bot()
    bot.context.bot_data["db"] = conn  # o cenário com cartão e dívidas deste arquivo
    bot.send(strategy.strategy_command, "/estrategia sobra", ["sobra"])
    assert "cadastre o salário primeiro" in bot.last
    with_salary(conn)
    bot.send(strategy.strategy_command, "/estrategia sobra 3000", ["sobra", "3000"])
    assert "Reserva para viver" in bot.last and "R$ 3.000,00/mês" in bot.last


def test_calendario_das_faturas(conn):
    assert cc.invoice_schedule(conn, card(conn), TODAY) == [209_284, 181_486]
    assert cc.revolving_cents(conn, card(conn), TODAY) == 0
    assert cc.pre_bot_cents(conn, card(conn), TODAY) == 209_284


def test_fatura_vencida_vira_rotativo_na_estrategia(conn):
    after_due = date(2026, 12, 11)  # dezembro venceu sem pagamento
    assert cc.revolving_cents(conn, card(conn), after_due) == 209_284
    [rot] = strategy.revolving_inputs(conn, after_due)
    assert (rot.name, rot.balance_cents, rot.rate_ppm) == ("Nubank (rotativo)", 209_284, 161_000)
    # Depois do lançamento automático do rotativo, a parte com juros continua contando.
    cc.roll_overdue(conn, card(conn), after_due, NOW)
    # 2.092,84 × 16,1% = 336,947 -> 336,95 de juros -> 2.429,79 na fatura de janeiro
    assert cc.revolving_cents(conn, card(conn), after_due) == 242_979
    # e o calendário da fatura de janeiro não conta essa parte duas vezes
    assert cc.invoice_schedule(conn, card(conn), after_due) == [181_486]


def test_avalanche_poe_o_rotativo_em_primeiro(conn):
    after_due = date(2026, 12, 11)
    inputs = strategy.strategy_inputs(debts.list_active(conn)) + strategy.revolving_inputs(
        conn, after_due
    )
    r = strategy.finance.run_strategy(inputs, "avalanche", extra_cents=300_000)
    assert r.payoff_order[0] == "Nubank (rotativo)"  # 16,1% a.m. > 8% do santander


def test_caixa_real_por_mes(conn):
    rows = strategy.monthly_outflow(debts.list_active(conn), conn, TODAY)
    # dez: parcela 793,21 + fatura 2.092,84 ; jan: 793,21 + 1.814,86
    assert rows[:2] == [("2026-12", 79_321, 209_284), ("2027-01", 79_321, 181_486)]
    text = "\n".join(strategy.format_outflow(rows))
    assert "dez/2026 R$ 2.886,05" in text  # 793,21 + 2.092,84
    assert "jan/2027 R$ 2.608,07" in text  # 793,21 + 1.814,86


def test_estrategia_mostra_as_faturas_e_sugere_semcartao():
    bot = Bot()
    today = datetime.now(SP).date()
    cc.create(bot.conn, cc.NewCard("Nubank", 3, 10, 960_000, 100_000, 50_000), today, NOW)
    debts.create(
        bot.conn,
        debts.NewDebt(
            "santander", "cheque_especial", 128_640, rate_ppm=80_000, payment_cents=20_000
        ),
        NOW,
    )
    bot.send(strategy.strategy_command)
    assert "Faturas dos cartões (sem juros" in bot.last
    assert "/semcartao" in bot.last
    bot.send(strategy.no_card_command)
    assert "Se você PARAR de usar os cartões" in bot.last
    assert "sem mês definido" in bot.last
    bot.close()


def test_cenario_sem_cartao_libera_dinheiro_para_as_dividas(conn):
    debt_inputs = strategy.strategy_inputs(debts.list_active(conn))
    cards = strategy.card_inputs(conn, TODAY)
    [nubank] = cards
    assert (nubank.balance_cents, nubank.schedule) == (959_454, (209_284, 181_486))
    baseline = strategy.finance.run_strategy(debt_inputs, "avalanche")
    text = strategy.format_no_card_scenario(debt_inputs, cards, 0, TODAY, baseline)
    # Orçamento = parcela 793,21 + fatura de dezembro 2.092,84 = 2.886,05
    assert "separando R$ 2.886,05 por mês" in text
    # Sem o cenário, o santander (sem parcela) nunca recebe nada até o clt acabar;
    # com as faturas diminuindo, sobra dinheiro antes para ele.
    with_cards = strategy.finance.run_strategy(debt_inputs + cards, "avalanche")
    assert with_cards.payoff_month["santander"] < baseline.payoff_month["santander"]


def test_saude_conta_as_compras_de_antes_do_bot(conn):
    i = health.indicators(conn, TODAY, SP, HealthSettings())
    assert i.card_pre_bot_cents == 209_284
    # O comprometimento agora é só das dívidas; as faturas antigas ficam à parte,
    # mês a mês, e a sobra de cada mês desconta a fatura daquele mês.
    assert i.card_pre_bot_months[0] == ("2026-12", 209_284)
    assert i.leftover_by_month()[0] == ("2026-12", i.leftover_cents - 209_284)
    assert i.total_with_cards_cents == 128_640 + 237_900 + 959_454
    text = "\n".join(format_indicators(i, HealthSettings()))
    assert "Faturas de compras feitas antes do bot (não estão nos gastos" in text
    assert "dez/2026 R$ 2.092,84" in text
    # dívidas 1.286,40 + 2.379,00 = 3.665,40; + cartões 9.594,54 = 13.259,94
    assert "+ cartões R$ 9.594,54 = R$ 13.259,94" in text
