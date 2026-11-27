"""Testes do /simular e do /estrategia (textos sobre os cálculos de test_finance)."""

from datetime import UTC, date, datetime

from fakes import Bot

from finbot.bot import strategy
from finbot.services import debts, finance

TODAY = date(2026, 11, 26)
NOW = datetime(2026, 11, 26, tzinfo=UTC)


def _debt(**over):
    base = dict(
        id=1,
        name="Empréstimo",
        kind="emprestimo",
        creditor=None,
        balance_cents=500_000,
        rate_ppm=30_000,
        payment_cents=30_000,
        due_day=10,
        remaining_installments=None,
        start_date=None,
        status="ativa",
        created_at=NOW,
    )
    return debts.Debt(**{**base, **over})


def test_simulacao_com_extra():
    # 5.000 a 3%: pagando 300 são 24 meses; pagando 400 são 16 (contas em test_finance)
    d = _debt()
    s = finance.simulate_extra(d.balance_cents, d.rate_ppm, d.payment_cents, 10_000)
    text = strategy.format_simulation(d, 10_000, s, TODAY)
    assert "Hoje: R$ 300,00/mês → quitação em nov/2028 (24 pagamento(s))" in text
    assert "Com extra: R$ 400,00/mês → quitação em mar/2028 (16 pagamento(s))" in text
    saved = s.interest_saved_cents
    assert f"8 mês(es) a menos e R$ {saved // 100:,}".replace(",", ".") in text


def test_simulacao_divida_crescendo_que_passa_a_diminuir():
    # 1.000 a 10% (juros 100) pagando 50: cresce. Com +100 (150/mês) passa a diminuir.
    d = _debt(balance_cents=100_000, rate_ppm=100_000, payment_cents=5_000)
    s = finance.simulate_extra(d.balance_cents, d.rate_ppm, d.payment_cents, 10_000)
    text = strategy.format_simulation(d, 10_000, s, TODAY)
    assert "a dívida CRESCE R$ 50,00 por mês" in text
    assert "passa a diminuir" in text


def test_simulacao_extra_insuficiente():
    d = _debt(balance_cents=100_000, rate_ppm=100_000, payment_cents=5_000)
    s = finance.simulate_extra(d.balance_cents, d.rate_ppm, d.payment_cents, 2_000)
    text = strategy.format_simulation(d, 2_000, s, TODAY)
    assert "mais de R$ 100,00 por mês" in text


# Mesmo exemplo de test_finance (feito à mão):
# X: 1.000 a 10%, parcela 100 | Y: 300 sem juros, parcela 100 | extra 100
# Avalanche: Y no mês 3, X no mês 6, juros 397,44
# Bola de neve: Y no mês 2, X no mês 6, juros 425,39 -> diferença 27,95
INPUTS = [
    finance.DebtInput("X", 100_000, 100_000, 10_000),
    finance.DebtInput("Y", 30_000, 0, 10_000),
]


def test_comparacao_de_estrategias():
    text = strategy.format_strategies(INPUTS, 10_000, TODAY)
    assert "Orçamento mensal: R$ 300,00 (parcelas R$ 200,00 + extra R$ 100,00)" in text
    # mês 3 a partir de nov/2026 = fev/2027; mês 6 = mai/2027; mês 2 = jan/2027
    assert "1. Y (sem juros): quitada em fev/2027 (mês 3)" in text
    assert "2. X (10,00% a.m.): quitada em mai/2027 (mês 6)" in text
    assert "Tudo quitado em mai/2027 (6 meses) · juros: R$ 397,44" in text
    assert "1. Y (sem juros): quitada em jan/2027 (mês 2)" in text
    assert "juros: R$ 425,39" in text
    assert "A avalanche paga R$ 27,95 a menos de juros." in text
    assert "A bola de neve quita a primeira dívida 1 mês(es) antes" in text


def test_estrategia_inviavel_mostra_os_juros():
    text = strategy.format_strategies([finance.DebtInput("Z", 100_000, 100_000, 5_000)], 0, TODAY)
    assert "não acabam" in text
    assert "Os juros do primeiro mês somam R$ 100,00" in text


def test_sem_dividas():
    assert "Nenhuma dívida" in strategy.format_strategies([], 0, TODAY)


def test_cartao_zerado_fica_fora_da_estrategia():
    items = [_debt(), _debt(id=2, name="Cartão", kind="cartao", balance_cents=0)]
    assert [d.name for d in strategy.strategy_inputs(items)] == ["Empréstimo"]


def test_comandos_pelo_chat():
    bot = Bot()
    debts.create(
        bot.conn,
        debts.NewDebt("Empréstimo", "emprestimo", 500_000, rate_ppm=30_000, payment_cents=30_000),
        NOW,
    )
    bot.send(strategy.simulate_command, args=["empréstimo", "100"])
    assert "8 mês(es) a menos" in bot.last
    bot.send(strategy.simulate_command, args=["itau", "100"])
    assert "Não encontrei" in bot.last
    bot.send(strategy.strategy_command, args=["200"])
    assert "extra R$ 200,00" in bot.last
    bot.send(strategy.strategy_command, args=["muito"])
    assert "Não entendi" in bot.last
    bot.close()
