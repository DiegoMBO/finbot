"""Salário pelo "chat": cadastro, horas extras, /desfazer, 13º, férias, edição,
mensagem do lançamento automático e o salário como renda no /saude."""

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fakes import Bot, FakeMessage, FakeQuery

from finbot.bot import handlers
from finbot.bot import salary as sb
from finbot.config import HealthSettings
from finbot.services import debts, health, salary

SP = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 11, 27, 15, tzinfo=UTC)
ADMISSION = date(2026, 7, 6)


@pytest.fixture
def bot():
    b = Bot()
    debts.create(
        b.conn,
        debts.NewDebt("clt-nubank", "emprestimo", 237_900, payment_cents=79_321, due_day=5),
        NOW,
    )
    yield b
    b.close()


def answer(bot, text):
    msg = FakeMessage(text, bot.log)
    return asyncio.run(sb.on_answer(SimpleNamespace(effective_message=msg), bot.context))


def tap(bot, handler, data):
    query = FakeQuery(data, FakeMessage(None, bot.log))
    update = SimpleNamespace(callback_query=query, effective_message=query.message)
    return asyncio.run(handler(update, bot.context))


def register_salary(conn):
    salary.save_employment(conn, ADMISSION, salary.parse_schedule("30/06, 30/11, 18/12"), NOW)
    salary.add_profile(
        conn,
        ADMISSION,
        NOW,
        gross_cents=450_000,
        weekly_hours=40,
        overtime_pct=50,
        payday_rule="5du",
        overtime_lag_months=2,
    )
    clt = debts.find(conn, "clt-nubank")[0]
    salary.add_deduction(conn, "consignado clt-nubank", NOW, debt_id=clt.id)


def test_cadastro_pelo_chat(bot):
    state = bot.send(sb.salary_command)
    assert state == sb.ASKING and "salário BRUTO" in bot.last
    for text in ["4.500", "06/07/2026", "40", "pular", "pular", "5º dia útil", "pular",
                 "30/06, 30/11, 18/12", "pular", "2", "clt", "pular"]:  # fmt: skip
        state = answer(bot, text)
    assert state == sb.CONFIRM
    assert "R$ 4.500,00 − INSS R$ 431,51 − IR R$ 0,00 = R$ 4.068,49" in bot.last
    tap(bot, sb.on_confirm, f"{sb.CB}save")
    assert "Salário cadastrado" in bot.last
    assert "Renda base (/saude): R$ 4.068,49" in bot.last
    [d] = salary.deductions(bot.conn)
    assert d.debt_id == debts.find(bot.conn, "clt")[0].id
    # Com cadastro, /salario mostra o resumo em vez de recomeçar
    bot.send(sb.salary_command)
    assert bot.last.startswith("💼 Salário")


def test_resposta_invalida_repete_a_pergunta(bot):
    bot.send(sb.salary_command)
    answer(bot, "muito")
    assert "Não entendi" in bot.log[-2][0]
    assert "salário BRUTO" in bot.last


def test_hora_extra_sem_salario_pede_cadastro(bot):
    bot.send(handlers.register, "fiz 2 horas extras")
    assert "cadastre o salário primeiro" in bot.last


def test_hora_extra_pelo_chat_e_desfazer(bot):
    register_salary(bot.conn)
    bot.send(handlers.register, "fiz 10 horas extras")
    assert "10h extras (50%)" in bot.last
    assert "= R$ 337,50 brutos" in bot.last  # 10 × 22,50 × 1,5
    assert len(salary.overtime_entries(bot.conn)) == 1
    bot.send(handlers.undo)
    assert "Apagado: 10h00 extras" in bot.last
    assert salary.overtime_entries(bot.conn) == []


def test_lista_de_horas_extras(bot):
    register_salary(bot.conn)
    salary.add_overtime(bot.conn, date(2026, 11, 20), 600, 50, NOW)
    profile = salary.profile_at(bot.conn, date(2026, 11, 27))
    text = sb.format_overtime(bot.conn, profile, date(2026, 11, 27))
    assert "Hora normal R$ 22,50 · 50% = R$ 33,75 · domingo/feriado 100% = R$ 45,00" in text
    assert "nov/2026: 10h → R$ 337,50 + DSR R$ 102,72 = R$ 440,22 · a receber em 07/01/2027" in text


def test_13_pelo_texto(bot):
    register_salary(bot.conn)
    text = sb.format_thirteenth(bot.conn, 2026, date(2026, 11, 27))
    assert "Meses que contam: 6/12" in text
    assert "30/11 13º (1ª parcela): R$ 937,50 (sem descontos)" in text
    assert "18/12 13º (2ª parcela): R$ 1.134,31" in text


def test_ferias_marcar_e_apagar(bot):
    register_salary(bot.conn)
    bot.send(sb.vacation_command, args=["marcar", "15/07/2027", "30"])
    assert "Férias marcadas" in bot.last
    assert "recebe R$ 4.973,39 em 13/07/2027" in bot.last
    bot.send(sb.vacation_command, args=["marcar", "01/03/2027", "30"])
    assert "ainda não completou 12 meses" in bot.last
    bot.send(sb.vacation_command, args=["apagar"])
    assert "apagadas" in bot.last
    assert len(salary.vacations(bot.conn)) == 1


def test_editar_salario(bot):
    register_salary(bot.conn)
    today = date(2026, 11, 27)
    text = sb.apply_edit(bot.conn, "raise", "5.000 01/01/2027", today, NOW)
    assert "R$ 5.000,00 a partir de 01/01/2027" in text
    assert salary.profile_at(bot.conn, date(2027, 1, 1)).gross_cents == 500_000
    sb.apply_edit(bot.conn, "advance", "40% dia 20", today, NOW)
    assert salary.profile_at(bot.conn, today).advance_pct == 40
    sb.apply_edit(bot.conn, "advance", "não", today, NOW)
    assert salary.profile_at(bot.conn, today).advance_pct is None
    sb.apply_edit(bot.conn, "deductions", "vale-transporte 6%", today, NOW)
    names = [d.name for d in salary.deductions(bot.conn)]
    assert names == ["consignado clt-nubank", "vale-transporte"]  # consignado continua


def test_mensagem_do_lancamento_e_desfazer_pelo_chat(bot):
    register_salary(bot.conn)
    pending = salary.pending_posts(bot.conn, date(2026, 12, 5))
    results = [salary.post(bot.conn, u, datetime(2026, 12, 5, 12, tzinfo=UTC), SP) for u in pending]
    text = sb.format_post(results[1])
    assert "💰 Caiu: salário de nov/2026 (05/12/2026)" in text
    assert "Consignado clt-nubank: −R$ 793,21" in text
    assert "Na conta: R$ 3.275,28" in text
    assert "/decimo recebi" in sb.format_post(results[0])
    bot.send(handlers.undo)
    assert "Desfeito: salário" in bot.last
    assert debts.find(bot.conn, "clt")[0].balance_cents == 237_900


def test_saude_usa_o_salario_como_renda(bot):
    register_salary(bot.conn)
    i = health.indicators(bot.conn, date(2026, 11, 27), SP, HealthSettings())
    assert (i.salary_cents, i.income_cents) == (406_849, 406_849)
    # Comprometimento: parcela do consignado 793,21 ÷ 4.068,49 = 19,5%
    assert i.commitment_bp == 79_321 * 10_000 // 406_849


def test_aviso_do_prazo_das_ferias(bot):
    register_salary(bot.conn)
    # Prazo do 1º período: 05/07/2028. 60 dias antes: 06/05/2028.
    assert "05/07/2028 (60 dias)" in sb.vacation_deadline_warning(bot.conn, date(2028, 5, 6))
    assert sb.vacation_deadline_warning(bot.conn, date(2028, 5, 7)) is None
    salary.add_vacation(bot.conn, date(2027, 8, 1), 30, 0, NOW)
    assert sb.vacation_deadline_warning(bot.conn, date(2028, 5, 6)) is None  # já tirou
