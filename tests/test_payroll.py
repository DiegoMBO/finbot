"""Cálculos da folha (CLT, tabelas de 2026), conferidos com contas feitas à mão.

Caso real usado nos testes: admissão em 06/07/2026, salário bruto R$ 4.500,
jornada de 40h (divisor 200), adicional de 50%, sem dependentes.
"""

from datetime import date
from decimal import Decimal

import pytest

from finbot.services import payroll as p
from finbot.services.payroll_tables import tables_for

ADMISSION = date(2026, 7, 6)


# --- INSS -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gross", "expected"),
    [
        # 1.621 × 7,5% = 121,575 -> 121,58
        (162_100, 12_158),
        # R$ 4.500: 121,575 + 1.281,84×9% (115,3656) + 1.451,43×12% (174,1716)
        #           + 145,73×14% (20,4022) = 431,5144 -> 431,51
        (450_000, 43_151),
        # 13º de R$ 2.250: 121,575 + 629×9% (56,61) = 178,185 -> 178,19
        (225_000, 17_819),
        # Acima do teto (8.475,55) paga o mesmo que no teto
        (2_000_000, p.inss(847_555, 2026)),
    ],
)
def test_inss_progressivo(gross, expected):
    assert p.inss(gross, 2026) == expected


# --- IRRF -----------------------------------------------------------------------


def test_ir_salario_4500_zerado_pela_reducao():
    # Simplificado (607,20) > INSS (431,51): base 4.500 − 607,20 = 3.892,80
    # 22,5%: 875,88 − 675,49 = 200,39; renda até 5.000 -> redução zera
    ir = p.irrf(450_000, 43_151, 0, 2026)
    assert (ir.base_cents, ir.used_simplified) == (389_280, True)
    assert (ir.table_tax_cents, ir.reduction_cents, ir.tax_cents) == (20_039, 20_039, 0)


def test_ir_na_faixa_de_reducao_parcial():
    # Férias de R$ 6.000: INSS 641,51 (> simplificado) -> base 5.358,49
    # 27,5%: 1.473,58475 − 908,73 = 564,85475 -> 564,85
    # Redução: 978,62 − 0,133145 × 6.000 (798,87) = 179,75 -> IR 385,10
    ir = p.irrf(600_000, 64_151, 0, 2026)
    assert (ir.base_cents, ir.used_simplified) == (535_849, False)
    assert (ir.table_tax_cents, ir.reduction_cents, ir.tax_cents) == (56_485, 17_975, 38_510)


def test_ir_acima_de_7350_sem_reducao_e_dependentes():
    # R$ 10.000, 1 dependente: INSS no teto; deduções = INSS + 189,59
    contrib = p.inss(1_000_000, 2026)
    ir = p.irrf(1_000_000, contrib, 1, 2026)
    assert ir.reduction_cents == 0
    assert ir.base_cents == 1_000_000 - contrib - 18_959


def test_ano_sem_tabela_usa_a_mais_recente():
    assert tables_for(2031).year == 2026
    assert tables_for(2020).year == 2026


# --- calendário -------------------------------------------------------------------


def test_pascoa_e_feriados():
    assert p.easter(2026) == date(2026, 4, 5)
    assert p.easter(2027) == date(2027, 3, 28)
    hol = p.holidays(2026)
    assert date(2026, 4, 3) in hol  # Sexta-feira Santa
    assert date(2026, 11, 20) in hol  # Consciência Negra (nacional desde 2024)
    assert date(2026, 1, 25) in p.holidays(2026, ((25, 1),))  # feriado municipal extra


def test_dias_de_descanso_novembro_2026():
    # Domingos 1, 8, 15, 22, 29 (5) + feriados em dia útil 02/11 e 20/11 (15/11 é domingo)
    assert p.rest_and_business_days(2026, 11) == (7, 23)


@pytest.mark.parametrize(
    ("year", "month", "rule", "expected"),
    [
        (2026, 12, "5du", date(2026, 12, 5)),  # ter 1, qua 2, qui 3, sex 4, sáb 5
        (2027, 1, "5du", date(2027, 1, 7)),  # 1º é feriado: sáb 2, seg 4, ter 5, qua 6, qui 7
        # Dia fixo em sábado/domingo/feriado antecipa para o dia útil BANCÁRIO (seg–sex)
        (2026, 11, "dia:15", date(2026, 11, 13)),  # 15/11 domingo (e feriado) -> sex 13
        (2027, 2, "dia:30", date(2027, 2, 26)),  # fevereiro: 28 é domingo, 27 sábado -> sex 26
        (2026, 11, "mes:30", date(2026, 11, 30)),  # 30/11/2026 é segunda
        (2026, 5, "mes:30", date(2026, 5, 29)),  # 30/05/2026 é sábado -> sex 29
    ],
)
def test_dia_do_pagamento(year, month, rule, expected):
    assert p.payday(year, month, rule) == expected


# --- horas extras ------------------------------------------------------------------


def test_valor_da_hora_40h():
    assert p.hourly_rate(450_000, 40) == Decimal(2_250)  # R$ 22,50
    assert p.hourly_rate(500_000, 44) == Decimal(500_000) / 220


def test_10_horas_extras_em_novembro_feito_a_mao():
    # 10h × 22,50 × 1,5 = 337,50 ; DSR = 337,50 ÷ 23 × 7 = 102,717 -> 102,72
    ot = p.overtime_month((2026, 11), {50: 600}, p.hourly_rate(450_000, 40))
    assert (ot.base_cents, ot.dsr_cents, ot.total_cents) == (33_750, 10_272, 44_022)


def test_horas_no_domingo_a_100_e_minutos():
    # 2h30 a 50% (2,5 × 33,75 = 84,375) + 4h a 100% (4 × 45 = 180) = 264,375 -> 264,38
    ot = p.overtime_month((2026, 11), {50: 150, 100: 240}, p.hourly_rate(450_000, 40))
    assert ot.base_cents == 26_438


@pytest.mark.parametrize(
    ("worked_on", "closing", "lag", "arrival"),
    [
        (date(2026, 11, 20), None, 2, (2027, 1)),  # fim do mês fecha; 2 meses depois
        (date(2026, 11, 20), 15, 2, (2027, 2)),  # depois do dia 15 vai para o ponto de dez
        (date(2026, 11, 10), 15, 1, (2026, 12)),
    ],
)
def test_mes_em_que_a_hora_extra_cai(worked_on, closing, lag, arrival):
    assert p.overtime_arrival(worked_on, closing, lag) == arrival


# --- 13º ------------------------------------------------------------------------------


def test_meses_do_13_regra_dos_15_dias():
    assert p.thirteenth_months(2026, ADMISSION) == 6  # 06/07: 26 dias em julho -> conta
    assert p.thirteenth_months(2026, date(2026, 7, 18)) == 5  # 14 dias em julho -> não conta
    assert p.thirteenth_months(2026, date(2026, 7, 17)) == 6  # exatamente 15 dias -> conta
    assert p.thirteenth_months(2026, ADMISSION, up_to_month=11) == 5
    assert p.thirteenth_months(2027, ADMISSION) == 12
    assert p.thirteenth_months(2025, ADMISSION) == 0


def test_13_de_2026_na_empresa_do_diego():
    # Empresa paga em 30/06 (adiantamento), 30/11 (resto da 1ª) e 18/12 (2ª).
    # Junho: admitido em julho -> nada. Novembro: 4.500 × 5/12 ÷ 2 = 937,50.
    # Dezembro: 13º = 4.500 × 6/12 = 2.250; INSS 178,19; IR isento (base < 2.428,80)
    #           2ª = 2.250 − 937,50 − 178,19 = 1.134,31
    plan = p.thirteenth_plan(2026, ADMISSION, 450_000, [(30, 6), (30, 11), (18, 12)])
    assert [(x.pay_date, x.kind, x.net_cents) for x in plan] == [
        (date(2026, 11, 30), "13-primeira", 93_750),
        (date(2026, 12, 18), "13-segunda", 113_431),
    ]
    assert (plan[1].gross_cents, plan[1].inss_cents, plan[1].irrf_cents) == (131_250, 17_819, 0)


def test_13_se_a_empresa_pagar_6_12_na_primeira():
    # Se cair 1.125,00 em 30/11, a 2ª fica 2.250 − 1.125 − 178,19 = 946,81
    plan = p.thirteenth_plan(
        2026, ADMISSION, 450_000, [(30, 6), (30, 11), (18, 12)], first_paid_override={11: 112_500}
    )
    assert [x.net_cents for x in plan] == [112_500, 94_681]


def test_13_de_2027_ano_inteiro():
    # 30/06: 4.500 × 6/12 ÷ 2 = 1.125 ; 30/11: 4.500 × 11/12 ÷ 2 = 2.062,50 − 1.125 = 937,50
    # 18/12: 13º = 4.500; INSS 431,51; IR 0 (redução) -> 4.500 − 2.062,50 − 431,51 = 2.005,99
    plan = p.thirteenth_plan(2027, ADMISSION, 450_000, [(30, 6), (30, 11), (18, 12)])
    assert [x.net_cents for x in plan] == [112_500, 93_750, 200_599]


# --- férias -------------------------------------------------------------------------


def test_ferias_30_dias_feito_a_mao():
    # 4.500 + 1/3 (1.500) = 6.000; INSS 641,51; IR 385,10 -> líquido 4.973,39
    v = p.vacation_pay(450_000, 30, 0, 0, 2026)
    assert (v.vacation_cents, v.third_cents, v.bonus_cents) == (450_000, 150_000, 0)
    assert (v.inss_cents, v.irrf_cents, v.net_cents) == (64_151, 38_510, 497_339)


def test_ferias_20_dias_vendendo_10_feito_a_mao():
    # Férias 3.000 + 1/3 1.000 = 4.000; INSS: 121,575 + 115,3656 + 1.097,16×12% (131,6592)
    #   = 368,5998 -> 368,60; IR: base 4.000 − 607,20 = 3.392,80 -> 15%: 508,92 − 394,16
    #   = 114,76, zerado pela redução. Abono: 1.500 + 1/3 = 2.000, sem descontos.
    # Líquido: 4.000 − 368,60 + 2.000 = 5.631,40
    v = p.vacation_pay(450_000, 20, 10, 0, 2026)
    assert (v.bonus_cents, v.inss_cents, v.irrf_cents, v.net_cents) == (200_000, 36_860, 0, 563_140)


def test_ferias_invalidas():
    for days, sold in ((4, 0), (30, 1), (20, 11)):
        with pytest.raises(ValueError):
            p.vacation_pay(450_000, days, sold, 0, 2026)


def test_periodos_de_ferias():
    [first] = p.vacation_periods(ADMISSION, date(2026, 11, 27))
    assert (first.acquisition_end, first.concession_deadline) == (
        date(2027, 7, 5),
        date(2028, 7, 5),
    )
    assert len(p.vacation_periods(ADMISSION, date(2027, 7, 6))) == 2


def test_dias_de_ferias_em_cada_mes():
    # 30 dias a partir de 15/07/2027: 17 em julho, 13 em agosto
    assert p.vacation_days_in_month(date(2027, 7, 15), 30, 2027, 7) == 17
    assert p.vacation_days_in_month(date(2027, 7, 15), 30, 2027, 8) == 13
    assert p.vacation_days_in_month(date(2027, 7, 15), 30, 2027, 9) == 0
