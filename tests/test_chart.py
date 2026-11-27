"""Testes do /grafico: séries (conferidas à mão) e geração da imagem."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from fakes import Bot

from finbot.bot import chart
from finbot.services import debts, evolution

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2026, 11, 26)
PNG = b"\x89PNG\r\n\x1a\n"


def _debt(**over):
    base = dict(
        id=1,
        name="Empréstimo",
        kind="emprestimo",
        creditor=None,
        balance_cents=485_000,
        rate_ppm=30_000,
        payment_cents=30_000,
        due_day=10,
        remaining_installments=None,
        start_date=None,
        status="ativa",
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    return debts.Debt(**{**base, **over})


def _event(day, balance, hour=15):
    return debts.DebtEvent(
        1, "pagamento", 0, 0, 0, balance, datetime(2026, 11, day, hour, tzinfo=UTC)
    )


def test_historico_um_ponto_por_dia_no_fuso_local():
    events = [
        _event(1, 500_000),
        _event(26, 490_000, hour=12),
        _event(26, 485_000, hour=18),  # mesmo dia: vale o último
        _event(27, 480_000, hour=2),  # 23h do dia 26 em São Paulo
    ]
    assert evolution.history_points(events, SP) == [
        (date(2026, 11, 1), 500_000),
        (date(2026, 11, 26), 480_000),
    ]


def test_projecao_feita_a_mao():
    # Hoje 4.850 a 3%, parcela 300, vence dia 10:
    # 10/12/2026: 4.850 + 145,50 − 300 = 4.695,50
    # 10/01/2027: 4.695,50 + 140,865 (-> 140,87) − 300 = 4.536,37
    ev = evolution.build(_debt(), [_event(1, 500_000)], TODAY, SP)
    assert ev.status == "quitavel"
    assert ev.projection[:3] == [
        (TODAY, 485_000),
        (date(2026, 12, 10), 469_550),
        (date(2027, 1, 10), 453_637),
    ]
    assert ev.projection[-1][1] == 0
    assert ev.payoff == ev.projection[-1][0]
    assert not ev.truncated


def test_divida_crescendo_projeta_24_meses():
    # 1.000 a 10%, parcela 50: 1.000 -> 1.050 -> 1.105 (1.050 + 105 − 50)
    ev = evolution.build(
        _debt(balance_cents=100_000, rate_ppm=100_000, payment_cents=5_000), [], TODAY, SP
    )
    assert ev.status == "crescendo"
    assert [p[1] for p in ev.projection[:3]] == [100_000, 105_000, 110_500]
    assert len(ev.projection) == 25  # hoje + 24 meses


def test_projecao_longa_e_cortada_em_10_anos():
    # 1.000 a 1% (juros 10) pagando 10,10: n = ln(101) / ln(1,01) = 4,615 / 0,00995 = 464 meses
    ev = evolution.build(
        _debt(balance_cents=100_000, rate_ppm=10_000, payment_cents=1_010), [], TODAY, SP
    )
    assert ev.truncated
    assert len(ev.projection) == evolution.MAX_PROJECTION_MONTHS + 1


def test_sem_projecao():
    assert evolution.build(_debt(payment_cents=None), [], TODAY, SP).projection == []
    card = _debt(kind="cartao", balance_cents=0)
    assert evolution.build(card, [], TODAY, SP).status == "em_dia"


def test_imagens_sao_png():
    for d in (
        _debt(),
        _debt(balance_cents=100_000, rate_ppm=100_000, payment_cents=5_000),
        _debt(payment_cents=None),
    ):
        ev = evolution.build(d, [_event(1, 500_000), _event(20, 490_000)], TODAY, SP)
        assert chart.render(d, ev).startswith(PNG)


def test_subtitulo():
    ev = evolution.build(_debt(), [], TODAY, SP)
    assert "quitação prevista em" in chart.subtitle(_debt(), ev)
    grow = _debt(balance_cents=100_000, rate_ppm=100_000, payment_cents=5_000)
    assert "CRESCE" in chart.subtitle(grow, evolution.build(grow, [], TODAY, SP))


def test_comando_envia_a_imagem():
    bot = Bot()
    debts.create(
        bot.conn,
        debts.NewDebt("Empréstimo", "emprestimo", 500_000, rate_ppm=30_000, payment_cents=30_000),
        datetime.now(UTC),
    )
    msg_log = bot.log
    bot.send(chart.chart_command, args=["empréstimo"])
    assert "📉 Empréstimo: Saldo atual R$ 5.000,00" in msg_log[-1][0]
    bot.send(chart.chart_command, args=[])
    assert "Use /grafico nome" in bot.last
    bot.close()
