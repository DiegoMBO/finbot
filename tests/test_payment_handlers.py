"""Pagamentos pelo "chat": texto livre, botões de dúvida, /pagar, /saldo e /desfazer."""

from datetime import UTC, datetime

import pytest
from fakes import Bot

from finbot.bot import handlers, payments
from finbot.services import debts, transactions

NOW = datetime.now(UTC)


@pytest.fixture
def bot():
    b = Bot()
    debts.create(
        b.conn,
        debts.NewDebt(
            "Empréstimo Nubank",
            "emprestimo",
            500_000,
            creditor="Nubank",
            rate_ppm=30_000,
            payment_cents=30_000,
        ),
        NOW,
    )
    debts.create(b.conn, debts.NewDebt("Cartão Nubank", "cartao", 100_000, creditor="Nubank"), NOW)
    debts.create(b.conn, debts.NewDebt("Carro", "financiamento", 2_000_000), NOW)
    yield b
    b.close()


def balance(bot, name):
    return debts.find(bot.conn, name)[0].balance_cents


def test_texto_livre_paga_a_divida_certa(bot):
    bot.send(handlers.register, "paguei 300 do empréstimo nubank")
    assert "✅ Pagamento de R$ 300,00 em Empréstimo Nubank" in bot.last
    assert "Juros do mês: R$ 150,00 · amortização: R$ 150,00" in bot.last
    assert "Saldo: R$ 5.000,00 → R$ 4.850,00" in bot.last
    assert 'gasto em "dívidas"' in bot.last
    assert balance(bot, "empréstimo nubank") == 485_000


def test_gasto_comum_continua_funcionando(bot):
    bot.send(handlers.register, "gastei 35 no mercado")
    assert "Gasto registrado: R$ 35,00 em alimentação" in bot.last


def test_ambiguo_pergunta_com_botoes_e_paga_a_escolhida(bot):
    bot.send(handlers.register, "paguei 200 nubank")
    assert "é de qual dívida?" in bot.last
    buttons = [b.text for row in bot.last_markup.inline_keyboard for b in row]
    assert sorted(buttons) == sorted(
        ["Empréstimo Nubank", "Cartão Nubank", "Não, é um gasto comum"]
    )
    cartao = debts.find(bot.conn, "cartão nubank")[0]
    bot.tap(handlers.payment_choice, f"{payments.CB}{cartao.id}")
    assert "Pagamento de R$ 200,00 em Cartão Nubank" in bot.last
    assert balance(bot, "cartão nubank") == 80_000
    assert balance(bot, "empréstimo nubank") == 500_000


def test_palavra_estranha_pergunta_e_gasto_comum_registra_normal(bot):
    bot.send(handlers.register, "paguei 100 de gasolina do carro")
    assert "é de qual dívida?" in bot.last
    bot.tap(handlers.payment_choice, f"{payments.CB}gasto")
    assert "Gasto registrado: R$ 100,00 em transporte" in bot.last
    assert balance(bot, "carro") == 2_000_000


def test_botao_expirado(bot):
    query = bot.tap(handlers.payment_choice, f"{payments.CB}gasto")
    assert query.answered == "Esse botão expirou."


def test_pagar_e_saldo_por_comando(bot):
    bot.send(payments.pay_command, args=["carro", "1.000"])
    assert "Pagamento de R$ 1.000,00 em Carro" in bot.last
    bot.send(payments.balance_command, args=["carro", "18.950,00"])
    assert (
        "Saldo de Carro atualizado: R$ 19.000,00 → R$ 18.950,00 (diferença -R$ 50,00)" in bot.last
    )
    bot.send(payments.pay_command, args=["nubank", "10"])
    assert "Mais de uma dívida combina" in bot.last
    bot.send(payments.pay_command, args=["itau", "10"])
    assert "Não encontrei" in bot.last


def test_desfazer_pagamento_pelo_chat(bot):
    bot.send(handlers.register, "paguei 300 do empréstimo nubank")
    bot.send(handlers.undo)
    assert "Apagado: pagamento de R$ 300,00 em Empréstimo Nubank" in bot.last
    assert "Saldo voltou para R$ 5.000,00" in bot.last
    assert balance(bot, "empréstimo nubank") == 500_000
    assert transactions.list_all(bot.conn) == []


def test_desfazer_bloqueado_explica(bot):
    bot.send(payments.pay_command, args=["carro", "1.000"])
    bot.send(payments.balance_command, args=["carro", "18.950"])
    bot.send(handlers.undo)
    assert "mudou depois disso" in bot.last
    assert balance(bot, "carro") == 1_895_000


def test_desfazer_gasto_comum_continua_igual(bot):
    bot.send(handlers.register, "gastei 35 no mercado")
    bot.send(handlers.undo)
    assert "Apagado: R$ 35,00 em alimentação (mercado)" in bot.last
