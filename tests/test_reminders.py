"""Testes dos lembretes de vencimento.

Hoje nos testes: quinta-feira, 26/11/2026. Janela padrão: 3 dias antes.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakes import Bot

from finbot import db
from finbot.bot import reminders as bot_reminders
from finbot.services import debts, reminders

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2026, 11, 26)
NOW = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def conn():
    c = db.connect(db.MEMORY)
    yield c
    c.close()


def loan(conn, name, due_day, payment=30_000, balance=500_000):
    return debts.create(
        conn,
        debts.NewDebt(name, "emprestimo", balance, payment_cents=payment, due_day=due_day),
        NOW,
    )


def names(items):
    return [r.debt.name for r in items]


def test_janela_de_3_dias(conn):
    loan(conn, "Hoje", 26)  # vence hoje: 0 dias
    loan(conn, "Domingo", 29)  # 3 dias
    loan(conn, "Segunda", 30)  # 4 dias: ainda não
    loan(conn, "Dezembro", 10)  # 14 dias
    assert names(reminders.pending(conn, TODAY, SP, 3)) == ["Hoje", "Domingo"]
    assert names(reminders.pending(conn, TODAY, SP, 4)) == ["Hoje", "Domingo", "Segunda"]


def test_parcela_sai_do_cadastro_e_limita_ao_saldo(conn):
    loan(conn, "Final", 28, payment=30_000, balance=12_000)
    [r] = reminders.pending(conn, TODAY, SP, 3)
    assert (r.due_date, r.amount_cents) == (date(2026, 11, 28), 12_000)


def test_avisa_uma_vez_so(conn):
    d = loan(conn, "Domingo", 29)
    reminders.mark_sent(conn, d.id, date(2026, 11, 29), datetime.now(UTC))
    assert reminders.pending(conn, TODAY, SP, 3) == []
    [r] = reminders.upcoming(conn, TODAY, SP)
    assert r.already_sent


def test_ja_pago_nao_avisa(conn):
    d = loan(conn, "Domingo", 29)
    debts.pay(conn, d, 30_000, datetime(2026, 11, 20, 15, tzinfo=UTC), SP)  # 9 dias antes
    assert reminders.pending(conn, TODAY, SP, 3) == []


def test_pagamento_antigo_nao_conta(conn):
    d = loan(conn, "Domingo", 29)
    debts.pay(conn, d, 30_000, datetime(2026, 10, 29, 15, tzinfo=UTC), SP)  # parcela de outubro
    assert names(reminders.pending(conn, TODAY, SP, 3)) == ["Domingo"]


def test_sem_vencimento_ou_saldo_zero_nao_avisa(conn):
    debts.create(conn, debts.NewDebt("Tia", "pessoa", 10_000), NOW)
    d = loan(conn, "Quitada", 28)
    debts.set_balance(conn, d, 0, NOW)
    assert reminders.upcoming(conn, TODAY, SP) == []


def test_textos():
    assert bot_reminders.when_text(date(2026, 11, 26), TODAY) == "vence HOJE (26/11, quinta)"
    assert bot_reminders.when_text(date(2026, 11, 27), TODAY) == "vence amanhã (27/11, sexta)"
    assert bot_reminders.when_text(date(2026, 11, 29), TODAY) == "vence em 3 dias (29/11, domingo)"


def test_envio_marca_como_enviado_e_nao_repete():
    bot = Bot()
    today = datetime.now(SP).date()
    due = today + timedelta(days=2)
    d = loan(bot.conn, "Empréstimo", due.day)
    # Se "daqui a 2 dias" virar o mês, o dia cadastrado ainda cai nessa data.
    assert reminders.next_due(bot.conn, d, today, SP).due_date == due

    sent = asyncio.run(bot_reminders.send_pending(bot.context))
    assert sent == 1
    chat_id, text = bot.sent[0]
    assert chat_id == 1  # ALLOWED_CHAT_ID dos testes
    assert text.startswith("⏰ Lembrete: Empréstimo vence em 2 dias")
    assert "Parcela: R$ 300,00." in text
    assert "paguei 300,00 do Empréstimo" in text

    assert asyncio.run(bot_reminders.send_pending(bot.context)) == 0  # não repete
    bot.send(bot_reminders.reminders_command)
    assert "• Empréstimo: vence em 2 dias" in bot.last
    assert "✓ avisado" in bot.last
    bot.close()


def test_agendamento_registra_as_tarefas(tmp_path):
    from finbot.bot.app import build_application
    from finbot.config import load_settings

    settings = load_settings(
        {"TELEGRAM_TOKEN": "1:a", "ALLOWED_CHAT_ID": "1", "DATABASE_PATH": str(tmp_path / "x.db")}
    )
    app = build_application(settings)
    jobs = {job.name for job in app.job_queue.jobs()}
    assert {"lembretes-diarios", "lembretes-ao-ligar"} <= jobs
    app.bot_data["db"].close()
