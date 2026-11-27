"""/extrato: entra − dívidas − faturas − gastos = saldo, mês a mês.

Cenário (parecido com o real):
- salário 4.500 bruto: 40% (1.800) no dia 15 e o resto no dia 30 do próprio mês;
  líquido 4.068,49 → no dia 30 caem 2.268,49 (antes do consignado);
- 13º: 937,50 em 30/11 e 1.134,31 em 18/12;
- clt-nubank 2.379,00, parcela 793,21 descontada no salário;
- Bradesco 2.500,00 sem juros, parcela 500,00 no dia 10;
- santander 1.286,40 a 8% a.m., sem parcela (cheque especial);
- cartão Nubank (fecha dia 3, vence dia 10): faturas dez 2.092,84 e jan 1.814,86.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from fakes import Bot

from finbot import db
from finbot.bot import cash as cash_bot
from finbot.parsing.message import ParsedEntry
from finbot.services import cash, debts, salary, transactions
from finbot.services import credit_cards as cc

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2026, 11, 27)
NOW = datetime(2026, 11, 27, 15, tzinfo=UTC)  # 12h em São Paulo


@pytest.fixture
def conn():
    c = db.connect(db.MEMORY)
    cc.create(
        c, cc.NewCard("Nubank", 3, 10, 960_000, 959_454, 209_284, (181_486,), 161_000), TODAY, NOW
    )
    salary.save_employment(c, date(2026, 7, 6), salary.parse_schedule("30/06, 30/11, 18/12"), NOW)
    salary.add_profile(
        c, date(2026, 7, 6), NOW, gross_cents=450_000, weekly_hours=40, overtime_pct=50,
        payday_rule="mes:30", advance_pct=40, advance_day=15, overtime_lag_months=2,
    )  # fmt: skip
    clt = debts.create(
        c, debts.NewDebt("clt-nubank", "emprestimo", 237_900, payment_cents=79_321, due_day=5), NOW
    )
    salary.add_deduction(c, "consignado clt-nubank", NOW, debt_id=clt.id)
    debts.create(
        c, debts.NewDebt("Bradesco", "emprestimo", 250_000, payment_cents=50_000, due_day=10), NOW
    )
    debts.create(c, debts.NewDebt("santander", "cheque_especial", 128_640, rate_ppm=80_000), NOW)
    yield c
    c.close()


def lines(s: cash.Statement, group: str) -> list[tuple]:
    return [(i.day, i.label, i.amount_cents, i.status) for i in s.items if i.group == group]


def test_saldo_inicial_e_meses_encadeados_feito_a_mao(conn):
    cash.set_anchor(conn, 35_000, NOW)  # R$ 350,00 na conta em 27/11
    nov = cash.statement(conn, 2026, 11, TODAY, SP)
    # Bradesco venceu em 10/11, antes do saldo inicial: já está dentro dos 350.
    assert lines(nov, cash.INCOME) == [
        (date(2026, 11, 30), "13º (1ª parcela)", 93_750, "⏳"),
        (date(2026, 11, 30), "salário de nov/2026", 226_849, "⏳"),
    ]
    assert lines(nov, cash.DEBT) == [(date(2026, 11, 30), "clt-nubank", 79_321, "⏳")]
    assert lines(nov, cash.INVOICE) == []
    # 3.205,99 − 793,21 = 2.412,78; 350 + 2.412,78 = 2.762,78
    assert (nov.result_cents, nov.end_cents) == (241_278, 276_278)
    # santander: 1.286,40 × 8% = 102,912 -> 102,91 (aumenta a dívida, fora da conta)
    assert [(g.name, g.balance_cents, g.interest_cents) for g in nov.growing] == [
        ("santander", 128_640, 10_291)
    ]

    dec = cash.statement(conn, 2026, 12, TODAY, SP)
    assert (dec.start_cents, dec.start_label) == (276_278, "Veio de novembro")
    # Entra: 1.800 + 1.134,31 + 2.268,49 = 5.202,80
    assert dec.total(cash.INCOME) == 520_280
    # Dívidas: Bradesco 500 (10/12) + clt 793,21 (30/12) = 1.293,21 ; fatura 2.092,84
    assert lines(dec, cash.DEBT) == [
        (date(2026, 12, 10), "Bradesco", 50_000, "⏳"),
        (date(2026, 12, 30), "clt-nubank", 79_321, "⏳"),
    ]
    assert lines(dec, cash.INVOICE) == [(date(2026, 12, 10), "fatura Nubank", 209_284, "⏳")]
    # 5.202,80 − 1.293,21 − 2.092,84 = 1.816,75 ; 2.762,78 + 1.816,75 = 4.579,53
    assert (dec.result_cents, dec.end_cents) == (181_675, 457_953)
    # santander em dez: 1.389,31 × 8% = 111,1448 -> 111,14
    assert [(g.balance_cents, g.interest_cents) for g in dec.growing] == [(138_931, 11_114)]


def test_consignado_acaba_na_ultima_parcela(conn):
    cash.set_anchor(conn, 0, NOW)
    # 2.379,00 = 793,21 (nov) + 793,21 (dez) + 792,58 (jan) -> em fev não aparece mais
    jan = cash.statement(conn, 2027, 1, TODAY, SP)
    assert [x for x in lines(jan, cash.DEBT) if x[1] == "clt-nubank"] == [
        (date(2027, 1, 29), "clt-nubank", 79_258, "⏳")  # 30/01/2027 é sábado -> sexta 29
    ]
    feb = cash.statement(conn, 2027, 2, TODAY, SP)
    assert [x for x in lines(feb, cash.DEBT) if x[1] == "clt-nubank"] == []


def test_consignado_que_comeca_na_folha_de_dezembro(conn):
    # O caso real: vencimentos jan, fev, mar -> descontos nas folhas de dez, jan, fev.
    conn.execute("UPDATE salary_deductions SET first_competence = '2026-12'")
    conn.commit()
    cash.set_anchor(conn, 200, NOW)
    nov = cash.statement(conn, 2026, 11, TODAY, SP)
    assert lines(nov, cash.DEBT) == []  # 30/11 sem desconto: caem os 2.268,49 inteiros
    # 2,00 + 937,50 + 2.268,49 = 3.207,99
    assert nov.end_cents == 320_799
    by_month = {}
    for y, m in [(2026, 12), (2027, 1), (2027, 2), (2027, 3)]:
        s = cash.statement(conn, y, m, TODAY, SP)
        by_month[m] = [x[2] for x in lines(s, cash.DEBT) if x[1] == "clt-nubank"]
    # 2.379,00 = 793,21 + 793,21 + 792,58; em março (folha de mar) já acabou
    assert by_month == {12: [79_321], 1: [79_321], 2: [79_258], 3: []}


def test_registros_depois_do_saldo_inicial(conn):
    cash.set_anchor(conn, 35_000, NOW)
    later = datetime(2026, 11, 28, 14, tzinfo=UTC)
    before = datetime(2026, 11, 27, 14, tzinfo=UTC)  # antes do saldo inicial: não conta
    transactions.add(conn, ParsedEntry("expense", 9_900, "lazer", "cinema"), before)
    transactions.add(conn, ParsedEntry("expense", 15_000, "alimentação", "mercado"), later)
    transactions.add(conn, ParsedEntry("income", 20_000, "outros", "freela"), later)
    # Compra no cartão: NÃO desconta de novo (vai para a fatura de dezembro)
    cc.add_purchase(
        conn, cc.list_cards(conn)[0], description="uber", category="transporte",
        purchased_on=date(2026, 11, 28), now=later, tz=SP, total_cents=3_000,
    )  # fmt: skip
    today = date(2026, 11, 28)
    nov = cash.statement(conn, 2026, 11, today, SP)
    assert (date(2026, 11, 28), "freela", 20_000, "✅") in lines(nov, cash.INCOME)
    assert lines(nov, cash.EXPENSE) == [(None, "alimentação", 15_000, "✅")]
    # 3.205,99 + 200 − 793,21 − 150 = 2.462,78
    assert nov.result_cents == 246_278
    dec = cash.statement(conn, 2026, 12, today, SP)
    # A fatura inteira: 2.092,84 + uber 30 = 2.122,84
    assert lines(dec, cash.INVOICE) == [(date(2026, 12, 10), "fatura Nubank", 212_284, "⏳")]


def test_pagamentos_feitos_e_atrasados(conn):
    cash.set_anchor(conn, 35_000, NOW)
    bradesco = next(d for d in debts.list_active(conn) if d.name == "Bradesco")
    paid_at = datetime(2026, 12, 10, 15, tzinfo=UTC)
    debts.pay(conn, bradesco, 50_000, paid_at, SP)
    today = date(2026, 12, 12)  # a fatura de 10/12 venceu e não foi paga
    dec = cash.statement(conn, 2026, 12, today, SP)
    assert (date(2026, 12, 10), "Bradesco", 50_000, "✅") in lines(dec, cash.DEBT)
    assert [x for x in lines(dec, cash.DEBT) if x[1] == "Bradesco"] == [
        (date(2026, 12, 10), "Bradesco", 50_000, "✅")  # não aparece de novo como previsto
    ]
    assert lines(dec, cash.INVOICE) == [(date(2026, 12, 10), "fatura Nubank", 209_284, "⚠️")]
    text = cash_bot.format_statement(dec, today)
    assert "venceu sem pagamento registrado" in text and "⚠️ atrasado" in text


def test_livre_de_verdade_e_ponto_mais_baixo_feito_a_mao(conn):
    # Com 350 na conta: novembro termina com 2.762,78, mas até o próximo dinheiro
    # (vale de 15/12) vencem Bradesco 500 + fatura Nubank 2.092,84 = 2.592,84.
    cash.set_anchor(conn, 35_000, NOW)
    o = cash.outlook(conn, 2026, 11, TODAY, SP)
    assert o.next_income_day == date(2026, 12, 15)
    assert [(i.label, i.day) for i in o.committed] == [
        ("Bradesco", date(2026, 12, 10)),
        ("fatura Nubank", date(2026, 12, 10)),
    ]
    assert (o.committed_cents, o.free_cents) == (259_284, 16_994)  # 2.762,78 − 2.592,84
    # Dia a dia: 27/11 350 → 30/11 2.762,78 → 10/12 169,94 → 15/12 1.969,94 ...
    assert (o.low_day, o.low_cents, o.negative) == (date(2026, 12, 10), 16_994, [])
    text = cash_bot.format_statement(cash.statement(conn, 2026, 11, TODAY, SP), TODAY, o)
    assert "Contas até o próximo dinheiro (15/12): −R$ 2.592,84" in text
    assert "(Bradesco 10/12, fatura Nubank 10/12)" in text
    # menos de R$ 300: ⚠️
    assert "⚠️ Sobram R$ 169,94 depois de pagar as contas até o próximo dinheiro (15/12)" in text
    assert "📉 Ponto mais baixo: R$ 169,94 em 10/12 (não fica negativo até 31/12)" in text


def test_fica_negativo_no_meio_do_caminho(conn):
    # Com só 2,00: 30/11 2.414,78 → 10/12 −178,06 → 15/12 +1.800 = 1.621,94
    cash.set_anchor(conn, 200, NOW)
    o = cash.outlook(conn, 2026, 11, TODAY, SP)
    assert o.free_cents == -17_806
    assert (o.low_day, o.low_cents) == (date(2026, 12, 10), -17_806)
    assert o.negative == [(date(2026, 12, 10), date(2026, 12, 14))]
    text = cash_bot.format_statement(cash.statement(conn, 2026, 11, TODAY, SP), TODAY, o)
    assert "🚨 Faltam R$ 178,06 para pagar as contas até o próximo dinheiro (15/12)" in text
    assert "Fica negativo de 10/12 a 14/12" in text


def test_aviso_das_parcelas_sem_mes_definido(conn):
    # Nubank: usado 9.594,54 − faturas dez 2.092,84 − jan 1.814,86 = 5.686,84 sem mês
    s = cash.statement(conn, 2026, 12, TODAY, SP)
    assert s.undated_cards == [("Nubank", 568_684)]
    text = cash_bot.format_statement(s, TODAY)
    assert (
        "⚠️ Cartões: R$ 5.686,84 de parcelas ainda sem mês definido\n   (Nubank R$ 5.686,84)" in text
    )
    assert "/fatura nome valor" in text


def test_rodape_aponta_para_os_meses_vizinhos(conn):
    nov = cash_bot.format_statement(cash.statement(conn, 2026, 11, TODAY, SP), TODAY)
    assert nov.endswith("Outro mês: /extrato dez")  # outubro já passou
    dec = cash_bot.format_statement(cash.statement(conn, 2026, 12, TODAY, SP), TODAY)
    assert dec.endswith("Outro mês: /extrato nov · /extrato jan")


def test_periodos_negativos_separados_por_virgula():
    o = cash.Outlook(
        None, [], -100, date(2026, 12, 10), -100,
        [(date(2026, 12, 10), date(2026, 12, 14)), (date(2026, 12, 16), date(2026, 12, 17)),
         (date(2027, 1, 10), date(2027, 1, 14))],
        date(2027, 1, 31),
    )  # fmt: skip
    text = "\n".join(cash_bot._format_outlook(o))
    assert "Fica negativo de 10/12 a 14/12, de 16/12 a 17/12 e de 10/01 a 14/01" in text


def test_sem_saldo_inicial_nao_tem_previsao_dia_a_dia(conn):
    assert cash.outlook(conn, 2026, 11, TODAY, SP) is None
    cash.set_anchor(conn, 200, NOW)
    assert cash.outlook(conn, 2026, 10, TODAY, SP) is None  # mês passado


def test_sem_saldo_inicial_mostra_so_o_resultado(conn):
    s = cash.statement(conn, 2026, 12, TODAY, SP)
    assert s.start_cents is None and s.end_cents is None
    assert s.result_cents == 181_675
    text = cash_bot.format_statement(s, TODAY)
    assert "= Resultado do mês (entra − sai): R$ 1.816,75 ✅" in text
    assert "/extrato inicial 350" in text


def test_texto_do_extrato(conn):
    cash.set_anchor(conn, -12_000, NOW)  # conta negativa: −120,00
    nov = cash.statement(conn, 2026, 11, TODAY, SP)
    text = cash_bot.format_statement(nov, TODAY)
    assert text.startswith("📒 Novembro/2026\nNa conta em 27/11 (você informou): −R$ 120,00")
    assert "   ⏳ 30/11 clt-nubank: R$ 793,21 (descontado no salário)" in text
    # −120 + 2.412,78 = 2.292,78
    assert "= Fim do mês: R$ 2.292,78 ✅" in text
    # O cheque especial vem em destaque, logo depois do saldo de partida
    assert text.index("🔥 PRIORIDADE: santander cresce R$ 102,91 neste mês") < text.index(
        "💰 Entra"
    )
    assert "Deve R$ 1.286,40 a 8% ao mês (≈ 151,8% ao ano)" in text
    assert "Sem pagar, vira R$ 2.041,35 em 6 meses e R$ 3.239,36 em 12." in text
    assert "Cada R$ 100 que você abate economiza R$ 8,00 por mês" in text


def test_projecao_do_cheque_especial_feita_a_mao():
    # 1.286,40 a 8% a.m., juros sobre juros, arredondando cada mês:
    # 1.389,31 · 1.500,45 · 1.620,49 · 1.750,13 · 1.890,14 · 2.041,35 (6 meses)
    # 2.204,66 · 2.381,03 · 2.571,51 · 2.777,23 · 2.999,41 · 3.239,36 (12 meses)
    g = cash.Growing("santander", 128_640, 10_291, 80_000)
    assert [g.projected(n) for n in (1, 2, 6, 12)] == [138_931, 150_045, 204_135, 323_936]
    assert g.annual_ppm == 1_518_170  # (1,08)^12 − 1 = 151,817%
    assert cash_bot.pct(g.rate_ppm) == "8%" and cash_bot.pct(g.annual_ppm) == "151,8%"
    # Com o que sobra, até quitar: 1.286,40 + 102,91 = 1.389,31 no máximo
    assert g.suggested_payment(16_994) == 16_994
    assert g.suggested_payment(500_000) == 138_931
    assert g.suggested_payment(-100) == 0


def test_sugestao_de_mandar_a_sobra_para_o_cheque_especial(conn):
    # Com 350: sobram 169,94 até 15/12 -> mandar 169,94 para o santander
    # 169,94 × 8% = 13,5952 -> economiza R$ 13,60 de juros por mês
    cash.set_anchor(conn, 35_000, NOW)
    s = cash.statement(conn, 2026, 11, TODAY, SP)
    o = cash.outlook(conn, 2026, 11, TODAY, SP)
    text = cash_bot.format_statement(s, TODAY, o)
    assert "💡 Com o que sobra, mande até R$ 169,94 para o santander (economiza R$ 13,60" in text
    # Quando falta, não sugere
    cash.set_anchor(conn, 200, NOW)
    o = cash.outlook(conn, 2026, 11, TODAY, SP)
    assert "💡" not in cash_bot.format_statement(s, TODAY, o)


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("dez", (2026, 12)),
        ("dezembro", (2026, 12)),
        ("jan", (2027, 1)),  # em novembro, janeiro é o do ano que vem
        ("próximo", (2026, 12)),
        ("anterior", (2026, 10)),
        ("12", (2026, 12)),
        ("3/2027", (2027, 3)),
        ("março", (2027, 3)),
    ],
)
def test_mes_do_extrato(arg, expected):
    assert cash_bot.parse_target(arg, TODAY) == expected


@pytest.mark.parametrize(
    ("text", "cents"), [("350", 35_000), ("-120", -12_000), ("−1.286,40", -128_640), ("0", 0)]
)
def test_valor_com_sinal(text, cents):
    assert cash_bot.parse_signed_amount(text) == cents


def test_comando_extrato():
    bot = Bot()
    bot.send(cash_bot.balance_command, "/extrato inicial", ["inicial"])
    assert "Informe quanto tem na conta agora" in bot.last
    bot.send(cash_bot.balance_command, "/extrato inicial 350", ["inicial", "350"])
    assert bot.last.startswith("✅ Saldo inicial: R$ 350,00")
    assert cash.get_anchor(bot.conn).amount_cents == 35_000
    bot.send(cash_bot.balance_command, "/extrato inicial", ["inicial"])
    assert bot.last.startswith("Saldo inicial: R$ 350,00, informado em")
    bot.send(cash_bot.balance_command, "/extrato xyz", ["xyz"])
    assert "não entendi o mês" in bot.last
    bot.send(cash_bot.balance_command, "/extrato próximo", ["próximo"])
    assert bot.last.startswith("📒 ")
