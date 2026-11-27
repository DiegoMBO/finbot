"""Testes do /saude, com um cenário de 4 meses calculado à mão.

Hoje: 26/11/2026. Meses completos: jul, ago, set, out.
Todo mês: salário 5.000; moradia 1.500; saúde 300; alimentação 800.
Lazer: jul 300, ago 400, set 440, out 620.  Outubro também tem: alimentação +40
(840, só +5%), "dívidas" 900 (pagamentos: não entram) e "pet" 200 (categoria nova).

Indicadores (base ago, set, out):
  renda média 5.000; fixos (moradia + saúde) 1.800
  parcelas: cheque 300 + empréstimo 945,60 = 1.245,60 (cartão fica de fora)
  comprometimento 1.245,60 / 5.000 = 24,912% -> "24,9%" -> saudável (até 30%)
  sobra 5.000 − 1.800 − 1.245,60 = 1.954,40
  total em dívidas 2.000 + 10.000 + 1.500 = 13.500

Sugestão (outubro contra a média de jul, ago, set):
  lazer: média (300 + 400 + 440) / 3 = 380; out 620 -> +240 (+63%)
  50% de 240 = 120 para o cheque especial (8% a.m., saldo 2.000, parcela 300):
    hoje:   n = −ln(1 − 160/300) / ln(1,08) = 0,762140 / 0,076961 = 9,90 -> 10 meses
    com 420: n = −ln(1 − 160/420) / ln(1,08) = 0,479573 / 0,076961 = 6,23 -> 7 meses
    -> 3 meses a menos
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from finbot import db
from finbot.bot.health import format_bp, format_indicators, format_suggestions
from finbot.config import ConfigError, HealthSettings, load_settings
from finbot.parsing.message import ParsedEntry
from finbot.services import debts, health, transactions

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2026, 11, 26)
CFG = HealthSettings()


def add(conn, kind, cents, category, month, year=2026):
    when = datetime(year, month, 15, 12, tzinfo=UTC)
    transactions.add(conn, ParsedEntry(kind, cents, category, ""), when)


@pytest.fixture
def conn():
    c = db.connect(db.MEMORY)
    yield c
    c.close()


@pytest.fixture
def scenario(conn):
    for month in (7, 8, 9, 10):
        add(conn, "income", 500_000, "salário", month)
        add(conn, "expense", 150_000, "moradia", month)
        add(conn, "expense", 30_000, "saúde", month)
        add(conn, "expense", 84_000 if month == 10 else 80_000, "alimentação", month)
    for month, cents in ((7, 30_000), (8, 40_000), (9, 44_000), (10, 62_000)):
        add(conn, "expense", cents, "lazer", month)
    add(conn, "expense", 90_000, "dívidas", 10)
    add(conn, "expense", 20_000, "pet", 10)

    now = datetime(2026, 7, 1, tzinfo=UTC)
    debts.create(
        conn,
        debts.NewDebt("Cheque", "cheque_especial", 200_000, rate_ppm=80_000, payment_cents=30_000),
        now,
    )
    debts.create(
        conn,
        debts.NewDebt("Empréstimo", "emprestimo", 1_000_000, rate_ppm=20_000, payment_cents=94_560),
        now,
    )
    debts.create(conn, debts.NewDebt("Cartão", "cartao", 150_000, payment_cents=50_000), now)
    return conn


def test_indicadores_feitos_a_mao(scenario):
    i = health.indicators(scenario, TODAY, SP, CFG)
    assert i.months == [(2026, 8), (2026, 9), (2026, 10)]
    assert not i.partial
    assert (i.income_cents, i.fixed_cents, i.installments_cents) == (500_000, 180_000, 124_560)
    assert sorted(i.installment_debts) == ["Cheque", "Empréstimo"]  # cartão fora
    assert (i.commitment_bp, i.band) == (2491, "saudável")
    assert i.leftover_cents == 195_440
    assert i.total_debt_cents == 1_350_000


def test_texto_dos_indicadores(scenario):
    text = "\n".join(format_indicators(health.indicators(scenario, TODAY, SP, CFG), CFG))
    assert "Base: média de ago/2026, set/2026, out/2026 (3 mês(es) completo(s))" in text
    assert "Gastos fixos médios (moradia, saúde, educação): R$ 1.800,00" in text
    assert (
        "Comprometimento da renda com dívidas: 24,9% → 🟢 saudável "
        "(saudável até 30%, atenção até 40%)" in text
    )
    assert "Sobra depois dos fixos e das parcelas: R$ 1.954,40" in text


def test_sugestoes_feitas_a_mao(scenario):
    r = health.suggestions(scenario, TODAY, SP, CFG)
    assert r.last_month == (2026, 10)
    assert r.base_months == [(2026, 7), (2026, 8), (2026, 9)]
    assert [s.category for s in r.items] == ["lazer", "pet"]  # alimentação (+5%) e dívidas fora
    lazer = r.items[0]
    assert (lazer.last_cents, lazer.average_cents, lazer.growth_cents, lazer.growth_pct) == (
        62_000,
        38_000,
        24_000,
        63,
    )
    assert lazer.redirect_cents == 12_000
    assert lazer.target.name == "Cheque"  # maior taxa
    assert (lazer.simulation.base.months, lazer.simulation.with_extra.months) == (10, 7)
    assert lazer.simulation.months_saved == 3
    pet = r.items[1]
    assert (pet.growth_pct, pet.average_cents) == (None, 0)  # categoria nova


def test_texto_das_sugestoes(scenario):
    text = "\n".join(format_suggestions(health.suggestions(scenario, TODAY, SP, CFG), CFG, TODAY))
    assert "1. lazer: R$ 620,00 em outubro contra média de R$ 380,00 (+R$ 240,00, +63%)." in text
    assert (
        "Mandando R$ 120,00/mês para Cheque (8,00% a.m., a maior taxa): quita 3 mês(es) antes"
        in text
    )
    assert "2. pet: R$ 200,00 em outubro, sem gastos nessa categoria nos meses anteriores." in text
    assert "pelo menos 20% e R$ 50,00; simulando 50% do aumento" in text


def test_limites_configuraveis_mudam_as_sugestoes(scenario):
    strict = HealthSettings(min_growth_pct=100)  # lazer cresceu 63%: sai
    r = health.suggestions(scenario, TODAY, SP, strict)
    assert [s.category for s in r.items] == ["pet"]


def test_so_mes_atual_e_parcial(conn):
    add(conn, "income", 300_000, "salário", 11)
    i = health.indicators(conn, TODAY, SP, CFG)
    assert (i.partial, i.months, i.income_cents) == (True, [(2026, 11)], 300_000)
    r = health.suggestions(conn, TODAY, SP, CFG)
    assert r.items == []
    assert "ainda não há meses anteriores" in format_suggestions(r, CFG, TODAY)[0]


def test_sem_renda(conn):
    add(conn, "expense", 10_000, "moradia", 10)
    i = health.indicators(conn, TODAY, SP, CFG)
    assert (i.commitment_bp, i.band) == (None, None)
    assert "sem entradas registradas" in "\n".join(format_indicators(i, CFG))


@pytest.mark.parametrize(
    ("bp", "band"),
    [(0, "saudável"), (3000, "saudável"), (3001, "atenção"), (4000, "atenção"), (4001, "alerta")],
)
def test_faixas(bp, band):
    assert health.classify(bp, 30, 40) == band


@pytest.mark.parametrize(
    ("bp", "text"), [(2491, "24,9%"), (2495, "25,0%"), (3000, "30,0%"), (5, "0,1%")]
)
def test_format_bp(bp, text):
    assert format_bp(bp) == text


# --- configuração -----------------------------------------------------------

BASE_ENV = {"TELEGRAM_TOKEN": "1:a", "ALLOWED_CHAT_ID": "1"}


def test_configuracao_padrao():
    s = load_settings(BASE_ENV)
    assert s.health == HealthSettings()
    assert (s.reminder_hour, s.reminder_days_before) == (9, 3)


def test_configuracao_personalizada():
    s = load_settings(
        {
            **BASE_ENV,
            "FIXED_CATEGORIES": "Moradia, transporte ,",
            "HEALTH_OK_MAX": "25",
            "HEALTH_WARN_MAX": "35",
            "SUGGEST_MIN_GROWTH_BRL": "100",
            "REMINDER_HOUR": "8",
        }
    )
    assert s.health.fixed_categories == ("moradia", "transporte")
    assert (s.health.ok_max_pct, s.health.warn_max_pct) == (25, 35)
    assert s.health.min_growth_cents == 10_000
    assert s.reminder_hour == 8


@pytest.mark.parametrize(
    ("env", "msg"),
    [
        ({"HEALTH_OK_MAX": "40", "HEALTH_WARN_MAX": "30"}, "menor que"),
        ({"HEALTH_OK_MAX": "trinta"}, "inteiro"),
        ({"REMINDER_HOUR": "24"}, "entre 0 e 23"),
    ],
)
def test_configuracao_invalida(env, msg):
    with pytest.raises(ConfigError, match=msg):
        load_settings({**BASE_ENV, **env})
