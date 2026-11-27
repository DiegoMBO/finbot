"""Serviço do salário com o caso real: admissão 06/07/2026, R$ 4.500, 40h,
50% de hora extra, pagamento no 5º dia útil, horas extras pagas 2 meses depois,
13º pago em 30/06, 30/11 e 18/12, consignado clt-nubank (R$ 793,21) em folha."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from finbot import db
from finbot.services import debts, salary, transactions

SP = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 11, 27, 15, tzinfo=UTC)
ADMISSION = date(2026, 7, 6)


@pytest.fixture
def conn():
    c = db.connect(db.MEMORY)
    salary.save_employment(c, ADMISSION, salary.parse_schedule("30/06, 30/11, 18/12"), NOW)
    salary.add_profile(
        c, ADMISSION, NOW, gross_cents=450_000, weekly_hours=40, overtime_pct=50,
        payday_rule="5du", overtime_lag_months=2,
    )  # fmt: skip
    clt = debts.create(
        c, debts.NewDebt("clt-nubank", "emprestimo", 237_900, payment_cents=79_321, due_day=5), NOW
    )
    salary.add_deduction(c, "consignado clt-nubank", NOW, debt_id=clt.id)
    yield c
    c.close()


def test_datas_do_13():
    assert salary.parse_schedule("18/12; 30/06,30/11") == [(30, 6), (30, 11), (18, 12)]
    with pytest.raises(ValueError):
        salary.parse_schedule("31/02")


def test_holerite_de_novembro(conn):
    s = salary.payslip(conn, (2026, 11))
    assert s.pay_date == date(2026, 12, 5)  # 5º dia útil de dezembro
    assert (s.gross_cents, s.inss_cents, s.ir.tax_cents) == (450_000, 43_151, 0)
    assert s.consigned_cents == 79_321
    assert (s.net_cents, s.received_cents) == (406_849, 327_528)  # 4.068,49 e 3.275,28


def test_mes_da_admissao_e_proporcional(conn):
    # Entrou dia 06: 30 − 5 = 25 dias comerciais -> 4.500 × 25/30 = 3.750
    assert salary.payslip(conn, (2026, 7)).salary_cents == 375_000
    assert salary.payslip(conn, (2026, 6)) is None  # antes da admissão


def test_horas_extras_entram_dois_meses_depois(conn):
    salary.add_overtime(conn, date(2026, 11, 20), 600, 50, NOW)
    assert salary.payslip(conn, (2026, 11)).overtime == []  # dezembro: ainda não
    s = salary.payslip(conn, (2026, 12))  # pago em 07/01/2027
    assert s.pay_date == date(2027, 1, 7)
    assert [o.total_cents for o in s.overtime] == [44_022]  # 337,50 + DSR 102,72
    # 4.940,22: INSS 121,575 + 115,3656 + 174,1716 + 585,95×14% (82,033) = 493,15
    assert (s.gross_cents, s.inss_cents, s.ir.tax_cents) == (494_022, 49_315, 0)


def test_13_previsto(conn):
    plan = salary.thirteenth_payments(conn, 2026)
    assert [(u.pay_date, u.kind, u.net_cents) for u in plan] == [
        (date(2026, 11, 30), "13-primeira", 93_750),
        (date(2026, 12, 18), "13-segunda", 113_431),
    ]


def test_13_com_media_das_horas_extras(conn):
    # Média: 440,22 ÷ 5 meses (jul–nov) = 88,04 -> 13º = 4.588,04 × 6/12 = 2.294,02
    # INSS 121,575 + 673,02×9% (60,5718) = 182,15 ; 2ª = 2.294,02 − 937,50 − 182,15
    salary.add_overtime(conn, date(2026, 11, 20), 600, 50, NOW)
    plan = salary.thirteenth_payments(conn, 2026)
    assert [u.net_cents for u in plan] == [93_750, 117_437]


def test_valor_real_da_primeira_parcela_ajusta_a_segunda(conn):
    salary.correct_first_thirteenth(conn, 2026, 11, 112_500, NOW)
    plan = salary.thirteenth_payments(conn, 2026)
    assert [u.net_cents for u in plan] == [112_500, 94_681]  # 2.250 − 1.125 − 178,19


def test_lancamento_automatico_e_desfazer(conn):
    dec5 = datetime(2026, 12, 5, 12, tzinfo=UTC)
    pending = salary.pending_posts(conn, date(2026, 12, 5))
    assert [(u.kind, u.pay_date) for u in pending] == [
        ("13-primeira", date(2026, 11, 30)),
        ("salario", date(2026, 12, 5)),
    ]
    results = [salary.post(conn, u, dec5, SP) for u in pending]
    assert salary.pending_posts(conn, date(2026, 12, 5)) == []  # não lança duas vezes

    clt = debts.find(conn, "clt-nubank")[0]
    assert clt.balance_cents == 237_900 - 79_321  # o consignado foi pago
    incomes = [t for t in transactions.list_all(conn) if t.kind == "income"]
    assert [(t.category, t.amount_cents) for t in incomes] == [
        ("13º salário", 93_750),
        ("salário", 406_849),
    ]

    salary_post = results[1]
    posted = salary.posted_by_transaction(conn, salary_post.transaction_id)
    salary.undo_post(conn, posted, SP)
    assert debts.find(conn, "clt-nubank")[0].balance_cents == 237_900
    assert [t.amount_cents for t in transactions.list_all(conn) if t.kind == "income"] == [93_750]
    assert salary.pending_posts(conn, date(2026, 12, 5)) == []  # desfeito: não relança


def test_lancamento_nao_volta_ao_passado(conn):
    # Cadastro em 27/11: o salário de outubro (pago 06/11) não é lançado.
    assert all(
        u.pay_date >= date(2026, 11, 27) for u in salary.pending_posts(conn, date(2026, 12, 1))
    )


def test_ferias_previstas_e_salario_seguinte_menor(conn):
    salary.add_vacation(conn, date(2027, 7, 15), 30, 0, NOW)
    [v] = salary.vacations(conn)
    up = salary.vacation_payment(conn, v)
    assert (up.pay_date, up.net_cents) == (date(2027, 7, 13), 497_339)  # conta em test_payroll
    # Julho: 17 dias de férias -> 13 trabalhados; agosto: 13 de férias -> 17 trabalhados
    assert salary.payslip(conn, (2027, 7)).salary_cents == 195_000
    assert salary.payslip(conn, (2027, 8)).salary_cents == 255_000


def test_reajuste_mantem_historico(conn):
    salary.raise_salary(conn, 500_000, date(2027, 1, 1), NOW)
    assert salary.payslip(conn, (2026, 12)).salary_cents == 450_000
    p = salary.payslip(conn, (2027, 1))
    assert (p.salary_cents, salary.profile_at(conn, date(2027, 1, 1)).weekly_hours) == (500_000, 40)


def test_descontos_e_renda_base(conn):
    assert salary.monthly_net_estimate(conn, date(2026, 11, 27)) == 406_849
    salary.add_deduction(conn, "vale-transporte", NOW, percent_bp=600)  # 6% de 4.500 = 270
    salary.add_deduction(conn, "plano de saúde", NOW, fixed_cents=15_000)
    assert salary.monthly_net_estimate(conn, date(2026, 11, 27)) == 406_849 - 27_000 - 15_000
    s = salary.payslip(conn, (2026, 11))
    assert s.deductions == [("vale-transporte", 27_000), ("plano de saúde", 15_000)]


def test_vale_quinzenal(conn):
    salary.update_settings(conn, advance_pct=40, advance_day=20)
    # Vale de 40%: 1.800 no dia 20; no 5º dia útil cai o resto do líquido
    found = salary.payments_between(conn, date(2026, 12, 1), date(2026, 12, 31))
    vale = next(u for u in found if u.kind == "vale")
    # 20/12/2026 é domingo: antecipa para sexta, 18/12
    assert (vale.pay_date, vale.net_cents) == (date(2026, 12, 18), 180_000)
    s = salary.payslip(conn, (2026, 11))
    assert s.net_cents == 406_849 - 180_000


def test_quinzena_dia_15_e_resto_dia_30_do_mesmo_mes(conn):
    # O caso real: R$ 1.800 (40%) no dia 15 e o resto, com descontos, no dia 30.
    salary.update_settings(conn, payday_rule="mes:30", advance_pct=40, advance_day=15)
    found = salary.payments_between(conn, date(2026, 12, 1), date(2026, 12, 31))
    assert [(u.kind, u.pay_date, u.received_cents) for u in found if u.kind != "13-segunda"] == [
        ("vale", date(2026, 12, 15), 180_000),  # terça
        # 4.068,49 − 1.800 = 2.268,49; − consignado 793,21 = 1.475,28 na conta
        ("salario", date(2026, 12, 30), 147_528),
    ]
    nov = salary.payslip(conn, (2026, 11))
    assert nov.pay_date == date(2026, 11, 30)  # salário de novembro cai em novembro
    assert (nov.net_cents, nov.received_cents) == (226_849, 147_528)
    # Horas extras de novembro, pagas "2 meses depois" = no pagamento de janeiro
    salary.add_overtime(conn, date(2026, 11, 20), 600, 50, NOW)
    assert salary.payslip(conn, (2026, 12)).overtime == []
    assert [o.total_cents for o in salary.payslip(conn, (2027, 1)).overtime] == [44_022]
