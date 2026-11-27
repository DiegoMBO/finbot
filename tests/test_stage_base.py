"""Etapa 1 do módulo de cartões: migração 003, /removerdivida e ajustes rápidos."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from fakes import Bot

from finbot import db
from finbot.bot import debt_form as form
from finbot.bot import debts as debt_bot
from finbot.bot.debts import describe_projection
from finbot.services import debts, transactions

NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)
SP = ZoneInfo("America/Sao_Paulo")
CARD_TABLES = {
    "cards",
    "invoices",
    "invoice_items",
    "card_purchases",
    "card_events",
    "card_reminders",
}


def test_migracao_003_cria_as_tabelas_de_cartao():
    conn = db.connect(db.MEMORY)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert CARD_TABLES <= tables
    assert db.schema_version(conn) == len(db.load_migrations())
    conn.close()


def test_cheque_especial_sem_pagamento_mostra_o_custo():
    # 1.286,40 a 8% = 102,912 -> R$ 102,91 por mês
    text = describe_projection(128_640, 80_000, None, None, date(2026, 11, 27))
    assert "cresce R$ 102,91 por mês" in text


def test_pergunta_do_credor_pede_o_nome():
    assert "NOME do credor" in form.step("creditor").question({})


@pytest.fixture
def bot():
    b = Bot()
    yield b
    b.close()


def test_remover_divida_com_confirmacao(bot):
    d = debts.create(bot.conn, debts.NewDebt("nubank", "cartao", 959_454), NOW)
    r = debts.pay(bot.conn, d, 10_000, NOW, SP)
    bot.send(debt_bot.remove_command, args=["nubank"])
    assert "Apagar a dívida 'nubank'" in bot.last
    bot.tap(debt_bot.remove_confirm, f"{debt_bot.REMOVE_CB}{d.id}:nao")
    assert "nada foi apagado" in bot.last
    assert debts.list_all(bot.conn)

    bot.tap(debt_bot.remove_confirm, f"{debt_bot.REMOVE_CB}{d.id}:sim")
    assert "apagada" in bot.last
    assert debts.list_all(bot.conn) == []
    assert debts.events(bot.conn, d.id) == []
    # O gasto do pagamento continua existindo.
    assert transactions.get(bot.conn, r.transaction_id).amount_cents == 10_000


def test_remover_divida_quitada_e_nome_desconhecido(bot):
    d = debts.create(bot.conn, debts.NewDebt("Tia", "pessoa", 5_000), NOW)
    debts.set_balance(bot.conn, d, 0, NOW)  # quitada
    bot.send(debt_bot.remove_command, args=["tia"])
    assert "Apagar a dívida 'Tia'" in bot.last
    bot.send(debt_bot.remove_command, args=["itau"])
    assert "Não encontrei" in bot.last
    bot.send(debt_bot.remove_command)
    assert "Use /removerdivida nome" in bot.last


def test_help_funciona_como_ajuda(tmp_path):
    from finbot.bot.app import build_application
    from finbot.config import load_settings

    settings = load_settings(
        {"TELEGRAM_TOKEN": "1:a", "ALLOWED_CHAT_ID": "1", "DATABASE_PATH": str(tmp_path / "x.db")}
    )
    app = build_application(settings)
    commands = set()
    for handler in app.handlers[0]:
        commands |= set(getattr(handler, "commands", ()))
    assert {"help", "ajuda", "start", "removerdivida"} <= commands
    app.bot_data["db"].close()
