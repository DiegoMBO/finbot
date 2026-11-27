"""Correções a partir do uso real (27/11/2026): busca com hífen, dica de cartão,
comando desconhecido e /saude sem renda."""

from datetime import UTC, date, datetime

import pytest
from fakes import Bot

from finbot.bot import chart, handlers
from finbot.bot import debts as debt_bot
from finbot.bot.health import format_indicators
from finbot.config import HealthSettings
from finbot.services import credit_cards as cc
from finbot.services import debts, health
from finbot.services.debts import search_words

NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)


@pytest.fixture
def bot():
    b = Bot()
    debts.create(
        b.conn, debts.NewDebt("clt-nubank", "emprestimo", 237_900, payment_cents=79_321), NOW
    )
    cc.create(b.conn, cc.NewCard("Nubank", 3, 10, 960_000), date(2026, 11, 27), NOW)
    yield b
    b.close()


def test_palavras_de_busca_separam_hifen():
    assert search_words("clt-nubank") == ["clt", "nubank"]
    assert search_words("Cartão_Mãe / casa.nova") == ["cartao", "mae", "casa", "nova"]


def test_busca_acha_nome_com_hifen(bot):
    assert [d.name for d in debts.find(bot.conn, "nubank")] == ["clt-nubank"]
    assert [d.name for d in debts.find(bot.conn, "clt")] == ["clt-nubank"]


def test_texto_livre_nao_confunde_divida_com_hifen_e_cartao(bot):
    # "clt-nubank" é UMA palavra no texto livre: não cita o cartão Nubank.
    bot.send(handlers.register, "paguei 793,21 do clt-nubank")
    assert "Pagamento de R$ 793,21 em clt-nubank" in bot.last


def test_grafico_de_cartao_explica(bot):
    debts.delete(bot.conn, debts.find(bot.conn, "clt")[0])
    bot.send(chart.chart_command, args=["nubank"])
    assert "Nubank é um cartão de crédito, não uma dívida: veja /cartao Nubank" in bot.last
    bot.send(debt_bot.show_debt, args=["nubank"])
    assert "/cartao Nubank" in bot.last


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("grafica", "grafico"), ("dividass", "dividas"), ("cartões", "cartoes"), ("xyz", None)],
)
def test_sugestao_de_comando(typed, expected):
    assert handlers.suggest_command(typed) == expected


def test_comando_desconhecido_responde(bot):
    bot.send(handlers.unknown_command, "/grafica nubank")
    assert (
        bot.last
        == "Não conheço o comando /grafica. Você quis dizer /grafico?\nTodos os comandos: /ajuda"
    )


def test_lista_de_comandos_bate_com_o_app(tmp_path):
    from telegram.ext import CommandHandler, ConversationHandler

    from finbot.bot.app import build_application
    from finbot.config import load_settings

    settings = load_settings(
        {"TELEGRAM_TOKEN": "1:a", "ALLOWED_CHAT_ID": "1", "DATABASE_PATH": str(tmp_path / "x.db")}
    )
    app = build_application(settings)
    registered = set()
    for h in app.handlers[0]:
        handlers_to_check = h.entry_points if isinstance(h, ConversationHandler) else [h]
        for inner in handlers_to_check:
            if isinstance(inner, CommandHandler):
                registered |= set(inner.commands)
    app.bot_data["db"].close()
    assert registered == set(handlers.KNOWN_COMMANDS)


def test_saude_sem_renda_nao_alarma(bot):
    i = health.indicators(
        bot.conn, date(2026, 11, 27), bot.context.bot_data["settings"].timezone, HealthSettings()
    )
    text = "\n".join(format_indicators(i, HealthSettings()))
    assert "Ainda não sei sua renda" in text
    assert "🚨" not in text and "Sobra" not in text
