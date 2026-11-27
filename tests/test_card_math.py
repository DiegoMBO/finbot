"""Cálculos do cartão conferidos com exemplos feitos à mão."""

from datetime import date

import pytest

from finbot.services import card_math as cm
from finbot.services.finance import present_value

# --- ciclo da fatura ----------------------------------------------------------


@pytest.mark.parametrize(
    ("purchase", "closing_day", "due_day", "closing", "due"),
    [
        # fecha dia 3, vence dia 10
        (date(2026, 12, 2), 3, 10, date(2026, 12, 3), date(2026, 12, 10)),
        (date(2026, 12, 3), 3, 10, date(2027, 1, 3), date(2027, 1, 10)),  # no dia: próxima
        (date(2026, 12, 31), 3, 10, date(2027, 1, 3), date(2027, 1, 10)),
        # fecha dia 28, vence dia 5 (vencimento no mês seguinte ao fechamento)
        (date(2026, 11, 27), 28, 5, date(2026, 11, 28), date(2026, 12, 5)),
        (date(2026, 11, 28), 28, 5, date(2026, 12, 28), date(2027, 1, 5)),
        # fecha dia 30: em fevereiro vira 28
        (date(2027, 2, 27), 30, 7, date(2027, 2, 28), date(2027, 3, 7)),
        (date(2027, 2, 28), 30, 7, date(2027, 3, 30), date(2027, 4, 7)),
    ],
)
def test_em_qual_fatura_cai_a_compra(purchase, closing_day, due_day, closing, due):
    c = cm.cycle_for_purchase(purchase, closing_day, due_day)
    assert (c.closing, c.due) == (closing, due)


def test_fechamento_de_uma_fatura_pelo_vencimento():
    assert cm.closing_before(date(2026, 12, 10), 3) == date(2026, 12, 3)
    assert cm.closing_before(date(2027, 1, 5), 28) == date(2026, 12, 28)


def test_parcelas_nas_faturas_seguintes():
    # Compra em 02/12 (fecha 3, vence 10) em 3x: dez, jan, fev
    first = cm.cycle_for_purchase(date(2026, 12, 2), 3, 10)
    dues = [cm.shift(first, k, 3, 10).due for k in range(3)]
    assert dues == [date(2026, 12, 10), date(2027, 1, 10), date(2027, 2, 10)]
    assert cm.shift(first, 2, 3, 10).reference_month == "2027-02"


def test_fatura_atual_e_a_proxima_a_vencer():
    # Hoje 27/11, vence dia 10: a fatura atual vence em 10/12 (fechou/fecha em 03/12)
    c = cm.payable_cycle(date(2026, 11, 27), 3, 10)
    assert (c.closing, c.due) == (date(2026, 12, 3), date(2026, 12, 10))
    # Em 05/12 a fatura de 10/12 já fechou, mas ainda é a atual (a pagar)
    c = cm.payable_cycle(date(2026, 12, 5), 3, 10)
    assert c.due == date(2026, 12, 10)


# --- parcelas -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "n", "parts"),
    [
        (10_000, 3, [3_334, 3_333, 3_333]),  # 100,00 em 3x
        (60_000, 3, [20_000, 20_000, 20_000]),
        (100, 1, [100]),
        (1_000, 7, [148, 142, 142, 142, 142, 142, 142]),  # 10,00 / 7 = 1,42 (+0,06 na 1ª)
    ],
)
def test_divisao_das_parcelas(total, n, parts):
    assert cm.split_installments(total, n) == parts
    assert sum(parts) == total


def test_divisao_invalida():
    with pytest.raises(ValueError):
        cm.split_installments(100, 0)
    with pytest.raises(ValueError):
        cm.split_installments(2, 3)


def test_juros_embutidos_feito_a_mao():
    # TV: R$ 3.000 à vista ou 10x de R$ 330 (R$ 3.300, R$ 300 de juros).
    # Fator a(i) = (1 − (1+i)^−10)/i tem de valer 3.000/330 = 9,0909:
    #   i = 1,75%: 1,0175^10 = 1,18945 -> (1 − 0,84073)/0,0175 = 9,1011
    #   i = 1,80%: 1,018^10  = 1,19530 -> (1 − 0,83661)/0,018  = 9,0772
    #   interpolando: 1,75% + 0,05% × (9,1011 − 9,0909)/(9,1011 − 9,0772) ≈ 1,7713%
    ppm = cm.implicit_rate_ppm(300_000, 33_000, 10)
    assert 17_700 <= ppm <= 17_730
    assert abs(present_value(33_000, ppm, 10) - 300_000) <= 10  # volta ao preço à vista


def test_sem_juros_quando_parcelas_somam_o_preco():
    assert cm.implicit_rate_ppm(300_000, 30_000, 10) == 0
    assert cm.implicit_rate_ppm(300_000, 29_000, 10) == 0


# --- rotativo e limite ----------------------------------------------------------


def test_rotativo_feito_a_mao():
    # Faltaram R$ 500 a 15%: juros R$ 75 -> R$ 575 na próxima fatura
    assert cm.revolving(50_000, 150_000) == (7_500, 57_500)
    assert cm.revolving(50_000, 0) == (0, 50_000)


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (70_000, 79_999, None),
        (70_000, 80_000, 80),
        (85_000, 90_000, 90),
        (70_000, 95_000, 90),  # pulou 80 e 90: vale o maior
        (95_000, 100_000, 100),
        (100_000, 110_000, None),  # já estava acima
    ],
)
def test_faixas_de_uso_do_limite(before, after, expected):
    assert cm.crossed_usage(before, after, 100_000) == expected


def test_uso_em_pontos_base():
    assert cm.usage_bp(959_454, 1_200_000) == 7_995  # 79,95%


def test_melhor_cartao_para_usar_hoje():
    # Hoje 02/12/2026.
    # A: fecha 3, vence 10 -> compra hoje vence 10/12 (fecha amanhã: pior)
    # B: fecha 1, vence 8  -> já fechou dia 1: compra hoje vence 08/01
    # C: fecha 28, vence 5 -> compra hoje (antes de 28/12) vence 05/01
    cards = [("A", 3, 10), ("B", 1, 8), ("C", 28, 5)]
    assert cm.best_card(cards, date(2026, 12, 2)) == ("B", date(2027, 1, 8))
    assert cm.best_card([], date(2026, 12, 2)) is None
