"""Testes dos cálculos financeiros, comparando com contas feitas À MÃO.

Cada exemplo traz a conta no comentário, para qualquer pessoa conferir com
uma calculadora. Valores em centavos; taxas em ppm (2% = 20000).
"""

from datetime import date
from decimal import Decimal

import pytest

from finbot.services import finance as f

# --- conversões de taxa -----------------------------------------------------


def test_rotativo_15_por_cento_ao_mes_da_435_por_cento_ao_ano():
    # 1,15² = 1,3225 ; 1,15⁴ = 1,74900625 ; 1,15⁸ = 3,0590228...
    # 1,15¹² = 1,15⁸ × 1,15⁴ = 5,3502500... -> 5,35025 − 1 = 4,35025 = 435,025%
    assert f.annual_equivalent_ppm(150_000) == 4_350_250


def test_12_por_cento_ao_ano_em_taxa_mensal():
    # 1,12^(1/12) = 1,0094888 -> 0,94888% a.m. -> 9489 ppm (arredondado)
    assert f.annual_to_monthly_ppm(120_000) == 9489


def test_ida_e_volta_mensal_anual():
    assert f.annual_to_monthly_ppm(f.annual_equivalent_ppm(20_000)) == 20_000


# --- juros de um mês --------------------------------------------------------


@pytest.mark.parametrize(
    ("balance", "rate", "expected"),
    [
        (1_000_000, 20_000, 20_000),  # R$ 10.000 × 2% = R$ 200,00
        (925_440, 20_000, 18_509),  # R$ 9.254,40 × 2% = 185,088 -> R$ 185,09
        (17_949, 100_000, 1_795),  # R$ 179,49 × 10% = 17,949 -> R$ 17,95
        (4_590, 100_000, 459),  # R$ 45,90 × 10% = R$ 4,59 exato
        (5, 100_000, 1),  # 0,5 centavo arredonda para cima (meio para cima)
        (4, 100_000, 0),  # 0,4 centavo arredonda para baixo
        (123_456, 0, 0),  # sem juros
    ],
)
def test_juros_do_mes(balance, rate, expected):
    assert f.monthly_interest(balance, rate) == expected


# --- parcela Price ----------------------------------------------------------


def test_parcela_price_exemplo_do_plano():
    # P = 10.000, i = 2%, n = 12
    # 1,02¹² = 1,268242 -> 1,02⁻¹² = 0,788493 -> 1 − 0,788493 = 0,211507
    # A = 10.000 × 0,02 / 0,211507 = 200 / 0,211507 = 945,596 -> R$ 945,60
    assert f.price_payment(1_000_000, 20_000, 12) == 94_560


def test_parcela_sem_juros_divide_e_arredonda_para_cima():
    assert f.price_payment(120_000, 0, 12) == 10_000  # 1.200 / 12 = 100
    assert f.price_payment(100_000, 0, 3) == 33_334  # 1.000 / 3 = 333,333... -> 333,34


def test_valor_presente_das_parcelas_volta_ao_saldo():
    # 12 × R$ 945,60 a 2% valem hoje ~R$ 10.000 (diferença só do arredondamento da parcela)
    assert abs(f.present_value(94_560, 20_000, 12) - 1_000_000) <= 10


# --- projeção ---------------------------------------------------------------


def test_projecao_price_primeiros_meses_feitos_a_mao():
    p = f.project(1_000_000, 20_000, 94_560)
    # Mês 1: juros 200,00 -> 10.000 + 200 − 945,60 = 9.254,40
    # Mês 2: juros 185,09 -> 9.254,40 + 185,09 − 945,60 = 8.493,89
    assert p.schedule[0] == f.Month(1, 20_000, 94_560, 925_440)
    assert p.schedule[1] == f.Month(2, 18_509, 94_560, 849_389)
    assert p.status == "quitavel"
    assert p.months == 12
    # Juros totais ≈ 12 × 945,60 − 10.000 = 1.347,20; a última parcela absorve
    # a diferença dos arredondamentos, então aceitamos alguns centavos.
    assert abs(p.total_interest_cents - 134_720) <= 10
    assert p.total_paid_cents == 1_000_000 + p.total_interest_cents


def test_prazo_exemplo_do_plano_24_pagamentos():
    # P = 5.000, i = 3%, A = 300 -> P·i = 150
    # n = −ln(1 − 150/300) / ln(1,03) = ln(2) / ln(1,03) = 0,693147 / 0,029559 = 23,45
    n = f.months_to_payoff_formula(500_000, 30_000, 30_000)
    assert round(n, 2) == Decimal("23.45")
    p = f.project(500_000, 30_000, 30_000)
    assert p.months == 24  # 23 parcelas cheias + 1 menor
    assert 0 < p.last_payment_cents < 30_000
    assert all(m.payment_cents == 30_000 for m in p.schedule[:-1])


def test_simulacao_bate_com_a_formula_fechada():
    # Para vários casos, os meses simulados = teto da fórmula fechada.
    cases = [(1_000_000, 20_000, 94_560), (500_000, 30_000, 40_000), (2_345_678, 15_900, 75_000)]
    for balance, rate, payment in cases:
        n = f.months_to_payoff_formula(balance, rate, payment)
        p = f.project(balance, rate, payment)
        assert abs(p.months - n) < 1.01, (balance, rate, payment, n, p.months)


def test_sem_juros_divide_simples():
    # 1.000 sem juros pagando 300: 300, 300, 300, 100 -> 4 meses
    p = f.project(100_000, 0, 30_000)
    assert (p.months, p.last_payment_cents, p.total_interest_cents) == (4, 10_000, 0)


def test_parcela_igual_aos_juros_estagna():
    # 1.000 a 10% = juros de 100; pagando 100 o saldo nunca diminui
    p = f.project(100_000, 100_000, 10_000)
    assert (p.status, p.months) == ("estagnada", None)


def test_parcela_menor_que_os_juros_cresce():
    # 1.000 a 10% = juros de 100; pagando 50 a dívida cresce 50 por mês
    p = f.project(100_000, 100_000, 5_000)
    assert (p.status, p.months, p.growth_per_month_cents) == ("crescendo", None, 5_000)


def test_sem_pagamento_e_quitada():
    assert f.project(100_000, 20_000, None).status == "sem_pagamento"
    assert f.project(0, 20_000, 10_000).status == "quitada"


def test_prazo_maior_que_o_horizonte():
    # Parcela só 1 centavo acima dos juros: leva séculos
    p = f.project(100_000, 100_000, 10_001, max_months=24)
    assert (p.status, p.months) == ("longa", None)


# --- valor extra (/simular) -------------------------------------------------


def test_simular_extra():
    # P = 5.000, i = 3%, A = 300 + 100 = 400
    # n = −ln(1 − 150/400) / ln(1,03) = −ln(0,625) / 0,029559 = 0,470004 / 0,029559 = 15,90 -> 16
    s = f.simulate_extra(500_000, 30_000, 30_000, 10_000)
    assert (s.base.months, s.with_extra.months, s.months_saved) == (24, 16, 8)
    assert s.interest_saved_cents == s.base.total_interest_cents - s.with_extra.total_interest_cents
    assert s.interest_saved_cents > 0


def test_simular_extra_tira_divida_do_crescimento():
    s = f.simulate_extra(100_000, 100_000, 5_000, 10_000)  # 50 + 100 = 150 > juros de 100
    assert s.base.status == "crescendo"
    assert s.with_extra.status == "quitavel"
    assert s.months_saved is None  # sem prazo de base, não há "economia" mensurável


# --- estratégias ------------------------------------------------------------
# X: R$ 1.000 a 10% a.m., parcela 100 | Y: R$ 300 sem juros, parcela 100
# Extra 100 -> orçamento fixo de 300 por mês.
X = f.DebtInput("X", 100_000, 100_000, 10_000)
Y = f.DebtInput("Y", 30_000, 0, 10_000)


def test_avalanche_feito_a_mao():
    # Alvo: X (maior juros).
    # M1: X 1000+100=1100; parcelas X->1000, Y->200; sobra 100 -> X 900
    # M2: X 900+90=990;    parcelas X->890,  Y->100; sobra 100 -> X 790
    # M3: X 790+79=869;    parcelas X->769,  Y->0 (quitada); sobra 100 -> X 669
    # M4: X 669+66,90=735,90; parcela 100 -> 635,90; sobra 200 -> X 435,90
    # M5: X 435,90+43,59=479,49; paga 300 -> 179,49
    # M6: X 179,49+17,95=197,44; paga 197,44 -> 0
    # Juros: 100 + 90 + 79 + 66,90 + 43,59 + 17,95 = 397,44
    r = f.run_strategy([X, Y], "avalanche", extra_cents=10_000)
    assert r.feasible
    assert r.monthly_budget_cents == 30_000
    assert r.payoff_month == {"Y": 3, "X": 6}
    assert r.payoff_order == ["Y", "X"]
    assert (r.total_months, r.total_interest_cents) == (6, 39_744)


def test_bola_de_neve_feito_a_mao():
    # Alvo: Y (menor saldo).
    # M1: X 1100, Y 300; parcelas X->1000, Y->200; sobra 100 -> Y 100
    # M2: X 1000+100=1100; parcelas X->1000, Y->0 (quitada); sobra 100 -> X 900
    # M3: X 900+90=990 -> paga 300 -> 690
    # M4: X 690+69=759 -> 459
    # M5: X 459+45,90=504,90 -> 204,90
    # M6: X 204,90+20,49=225,39 -> 0
    # Juros: 100 + 100 + 90 + 69 + 45,90 + 20,49 = 425,39
    r = f.run_strategy([X, Y], "bola_de_neve", extra_cents=10_000)
    assert r.payoff_month == {"Y": 2, "X": 6}
    assert (r.total_months, r.total_interest_cents) == (6, 42_539)


def test_comparar_avalanche_paga_menos_juros():
    avalanche, snowball = f.compare_strategies([X, Y], extra_cents=10_000)
    assert avalanche.total_interest_cents < snowball.total_interest_cents


def test_ordem_de_quitacao_no_mesmo_mes_segue_a_estrategia():
    # "Z" (10% a.m.) e "A" (sem juros) terminam no mesmo mês, sem parcela fixa,
    # com 200 de extra. Mês 1: Z vira 100 + 10 = 110; a avalanche paga Z primeiro
    # (110), e a sobra de 90 quita A (50). Ordem real: Z, depois A (não alfabética).
    debts = [f.DebtInput("A", 5_000, 0, 0), f.DebtInput("Z", 10_000, 100_000, 0)]
    r = f.run_strategy(debts, "avalanche", extra_cents=20_000)
    assert r.payoff_month == {"Z": 1, "A": 1}
    assert r.payoff_order == ["Z", "A"]
    assert r.total_interest_cents == 1_000


def test_estrategia_inviavel():
    # Juros de 100/mês e orçamento de 50: nunca quita
    r = f.run_strategy([f.DebtInput("Z", 100_000, 100_000, 5_000)], "avalanche", max_months=36)
    assert (r.feasible, r.total_months) == (False, None)


def test_estrategia_sem_orcamento():
    r = f.run_strategy([f.DebtInput("Z", 100_000, 0, 0)], "avalanche")
    assert not r.feasible


# --- datas ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "month", "day", "expected"),
    [
        (2027, 2, 31, date(2027, 2, 28)),
        (2028, 2, 31, date(2028, 2, 29)),  # ano bissexto
        (2026, 4, 31, date(2026, 4, 30)),
        (2026, 12, 10, date(2026, 12, 10)),
    ],
)
def test_vencimento_ajusta_fim_do_mes(year, month, day, expected):
    assert f.due_date(year, month, day) == expected


def test_proximo_vencimento():
    today = date(2026, 11, 26)
    assert f.next_due_date(today, 26) == date(2026, 11, 26)  # hoje conta
    assert f.next_due_date(today, 30) == date(2026, 11, 30)
    assert f.next_due_date(today, 10) == date(2026, 12, 10)


def test_data_de_quitacao():
    today = date(2026, 11, 26)
    # Vence dia 10: 1º pagamento 10/12/2026; 24º pagamento 10/11/2028
    assert f.payoff_date(today, 24, 10) == date(2028, 11, 10)
    # Vence dia 31: o 1º pagamento é 30/11/2026 (novembro só tem 30 dias),
    # e o 12º volta a ser dia 31, em 31/10/2027
    assert f.payoff_date(today, 12, 31) == date(2027, 10, 31)
    # Sem dia de vencimento: mês 1 = 26/12/2026; mês 3 = 26/02/2027
    assert f.payoff_date(today, 3, None) == date(2027, 2, 26)
