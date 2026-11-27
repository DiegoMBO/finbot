"""App web: login, Painel e dados do gráfico (mesmo cenário de test_cash.py).

Cenário: salário 4.500 (40% no dia 15, resto no dia 30), clt-nubank no salário,
Bradesco 500 no dia 10, santander sem parcela, cartão Nubank (dez 2.092,84).
"""

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from finbot import db
from finbot.config import load_settings
from finbot.services import cash, debts, salary
from finbot.services import credit_cards as cc
from finbot.web import views
from finbot.web.app import create_app
from finbot.web.config import WebSettings, load_web_settings
from finbot.web.security import LoginThrottle, hash_password, verify_password

TODAY = date(2026, 11, 27)
NOW = datetime(2026, 11, 27, 15, tzinfo=UTC)
PASSWORD = "senha-de-teste"


def build_scenario(conn):
    cc.create(
        conn,
        cc.NewCard("Nubank", 3, 10, 960_000, 959_454, 209_284, (181_486,), 161_000),
        TODAY,
        NOW,
    )
    salary.save_employment(
        conn, date(2026, 7, 6), salary.parse_schedule("30/06, 30/11, 18/12"), NOW
    )
    salary.add_profile(
        conn, date(2026, 7, 6), NOW, gross_cents=450_000, weekly_hours=40, overtime_pct=50,
        payday_rule="mes:30", advance_pct=40, advance_day=15, overtime_lag_months=2,
    )  # fmt: skip
    clt = debts.create(
        conn, debts.NewDebt("clt-nubank", "emprestimo", 237_900, payment_cents=79_321), NOW
    )
    salary.add_deduction(conn, "consignado clt-nubank", NOW, debt_id=clt.id)
    debts.create(
        conn,
        debts.NewDebt("Bradesco", "emprestimo", 250_000, payment_cents=50_000, due_day=10),
        NOW,
    )
    debts.create(conn, debts.NewDebt("santander", "cheque_especial", 128_640, rate_ppm=80_000), NOW)


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "finbot.db"
    conn = db.connect(path)
    build_scenario(conn)
    settings = load_settings(
        {"TELEGRAM_TOKEN": "1:a", "ALLOWED_CHAT_ID": "1", "DATABASE_PATH": str(path)}
    )
    web = WebSettings(
        password_hash=hash_password(PASSWORD), secret_path=tmp_path / "key", https_only=False
    )
    client = TestClient(create_app(settings, web, clock=lambda: NOW))
    yield conn, client
    conn.close()


def login(client):
    return client.post("/login", data={"senha": PASSWORD}, follow_redirects=False)


# --- segurança -------------------------------------------------------------------


def test_hash_da_senha():
    stored = hash_password("abc12345")
    assert "abc12345" not in stored and stored.startswith("scrypt$")
    assert verify_password("abc12345", stored)
    assert not verify_password("errada", stored)
    assert not verify_password("abc12345", "lixo")


def test_bloqueio_depois_de_5_erros():
    t = [0.0]
    throttle = LoginThrottle(clock=lambda: t[0])
    for _ in range(5):
        throttle.fail()
    assert throttle.seconds_locked() == 300
    t[0] = 301
    assert throttle.seconds_locked() == 0


def test_sem_login_vai_para_a_tela_de_login(env):
    _, client = env
    r = client.get("/", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/login")
    assert client.get("/api/saldo-diario").status_code == 401
    assert client.post("/saldo", data={"valor": "10"}, follow_redirects=False).status_code == 303


def test_login_errado_e_certo(env):
    _, client = env
    r = client.post("/login", data={"senha": "nao-e-essa"})
    assert r.status_code == 401 and "Senha errada" in r.text
    r = login(client)
    assert (r.status_code, r.headers["location"]) == (303, "/")
    assert client.get("/").status_code == 200
    client.post("/sair")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_cabecalhos_de_seguranca(env):
    _, client = env
    r = client.get("/login")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Cache-Control"] == "no-store"


def test_falta_a_senha_no_env():
    with pytest.raises(Exception, match="WEB_PASSWORD_HASH"):
        load_web_settings({})


# --- painel ----------------------------------------------------------------------


def test_painel_sem_saldo_inicial_pede_o_valor(env):
    _, client = env
    login(client)
    r = client.get("/")
    assert "Quanto você tem na conta agora?" in r.text
    assert client.get("/api/saldo-diario").json() == {"ready": False, "days": []}


def test_informar_saldo_pelo_app_vale_para_o_bot(env):
    conn, client = env
    login(client)
    r = client.post("/saldo", data={"valor": "350"})
    assert "Saldo da conta atualizado: R$ 350,00" in r.text
    assert cash.get_anchor(conn).amount_cents == 35_000  # o mesmo do /extrato inicial
    r = client.post("/saldo", data={"valor": "abc"})
    assert "Não entendi o valor" in r.text


def test_grafico_feito_a_mao(env):
    conn, client = env
    cash.set_anchor(conn, 35_000, NOW)
    login(client)
    data = client.get("/api/saldo-diario").json()
    by_day = {d["date"]: d for d in data["days"]}
    assert data["ready"] and len(data["days"]) == 91  # hoje + 90 dias
    # 27/11: 350 · 30/11: + 937,50 + 2.268,49 − 793,21 = 2.762,78
    assert by_day["2026-11-27"]["cents"] == 35_000
    assert by_day["2026-11-29"]["cents"] == 35_000
    assert by_day["2026-11-30"]["cents"] == 276_278
    # entradas primeiro no mesmo dia
    assert [i["cents"] for i in by_day["2026-11-30"]["items"]] == [93_750, 226_849, -79_321]
    # 10/12: − Bradesco 500 − fatura 2.092,84 = 169,94 · 15/12: + 1.800 = 1.969,94
    assert by_day["2026-12-10"]["cents"] == 16_994
    assert by_day["2026-12-15"]["cents"] == 196_994


def test_painel_feito_a_mao(env):
    conn, client = env
    cash.set_anchor(conn, 200, NOW)  # só R$ 2,00
    d = views.dashboard(conn, TODAY, load_settings_tz())
    tiles = {t.label: (t.value, t.tone) for t in d["tiles"]}
    assert tiles["Na conta hoje (estimado)"] == ("R$ 2,00", "neutral")
    # 2,00 + 2.412,78 = 2.414,78 no fim de nov; até 15/12 vencem 2.592,84 -> faltam 178,06
    assert tiles["Faltam até 15/12"] == ("R$ 178,06", "critical")
    assert tiles["Ponto mais baixo"] == ("−R$ 178,06", "critical")
    assert d["negative"][0] == (date(2026, 12, 10), date(2026, 12, 14))
    assert [(u["day"], u["label"]) for u in d["upcoming"][:3]] == [
        (date(2026, 11, 30), "clt-nubank"),
        (date(2026, 12, 10), "Bradesco"),
        (date(2026, 12, 10), "fatura Nubank"),
    ]
    login(client)
    html = client.get("/").text
    assert "Faltam até 15/12" in html and "R$ 178,06" in html
    assert "Fica negativo" in html and "de 10/12 a 14/12" in html
    assert "parcelas ainda sem mês definido" in html


def load_settings_tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/Sao_Paulo")


# --- etapa 2: registrar, desfazer, Gastos e Extrato --------------------------------


def post_entry(client, **fields):
    data = {"tipo": "expense", "voltar": "/gastos", **fields}
    return client.post("/registrar", data=data)


def test_registrar_gasto_entrada_e_desfazer(env):
    conn, client = env
    login(client)
    r = post_entry(client, valor="35,90", descricao="mercado")
    # categoria adivinhada pela descrição, como no bot
    assert "Gasto registrado: R$ 35,90 em alimentação (mercado)" in r.text
    r = post_entry(client, tipo="income", valor="200", descricao="freela")
    assert "Entrada registrada: R$ 200,00" in r.text
    from finbot.services import transactions

    assert [(t.kind, t.amount_cents) for t in transactions.list_all(conn)] == [
        ("expense", 3_590),
        ("income", 20_000),
    ]
    # Desfazer pelo app: a mesma regra do /desfazer (apaga a última, a entrada)
    r = client.post("/desfazer", data={"voltar": "/gastos"})
    assert "Apagado: R$ 200,00" in r.text
    assert [t.amount_cents for t in transactions.list_all(conn)] == [3_590]


def test_compra_parcelada_no_cartao_pelo_app(env):
    conn, client = env
    login(client)
    card = cc.list_cards(conn)[0]
    r = post_entry(client, valor="300", descricao="tênis", cartao=str(card.id), parcelas="3")
    assert "Compra no Nubank (em 3x): R$ 300,00" in r.text
    # 959.454 + 30.000 = 989.454 usado
    assert cc.list_cards(conn)[0].used_cents == 989_454
    # Comprado em 27/11 (fecha dia 3): 1ª parcela na fatura de dez
    assert "Faturas: dez/2026, jan/2027, fev/2027" in r.text
    r = client.post("/desfazer", data={"voltar": "/gastos"})
    assert "Desfeito: compra de R$ 300,00 no Nubank" in r.text
    assert cc.list_cards(conn)[0].used_cents == 959_454


def test_erros_do_formulario(env):
    _, client = env
    login(client)
    assert "Não registrei: valor inválido" in post_entry(client, valor="abc").text
    assert "não pode ser no futuro" in post_entry(client, valor="10", data="2026-11-28").text
    assert "parcelas: de 1 a 48" in post_entry(client, valor="10", cartao="1", parcelas="60").text


def test_gasto_em_outro_dia_e_alerta_de_orcamento(env):
    conn, client = env
    from finbot.services import budgets, transactions

    budgets.set_budget(conn, "alimentação", 5_000)  # R$ 50
    login(client)
    r = post_entry(client, valor="45", descricao="mercado", data="2026-11-20")
    assert "em 20/11" in r.text
    assert "90% do orçamento de alimentação" in r.text  # 45 de 50
    [t] = transactions.list_all(conn)
    assert t.created_at.astimezone(load_settings_tz()).date() == date(2026, 11, 20)


def test_tela_de_gastos(env):
    conn, client = env
    login(client)
    post_entry(client, valor="35,90", descricao="mercado")
    post_entry(client, valor="20", descricao="uber")
    html = client.get("/gastos").text
    assert "Gastos de novembro/2026" in html
    assert "R$ 55,90" in html  # total
    d = views.spending(conn, 2026, 11, TODAY, load_settings_tz())
    assert [(c["name"], c["cents"], c["pct"]) for c in d["categories"]] == [
        ("alimentação", 3_590, 64),  # 35,90 / 55,90 = 64%
        ("transporte", 2_000, 36),
    ]
    assert d["history"][-1] == {"month": "nov/2026", "expense": 5_590, "income": 0}
    assert d["nav"]["next"] is None  # não vai para o futuro nos gastos


def test_tela_do_extrato(env):
    conn, client = env
    cash.set_anchor(conn, 35_000, NOW)
    login(client)
    html = client.get("/extrato?mes=2026-12").text
    assert "Extrato de dezembro/2026" in html
    assert "Veio de novembro" in html and "R$ 2.762,78" in html
    assert "fatura Nubank" in html and "R$ 2.092,84" in html
    # 2.762,78 + 1.816,75 = 4.579,53 no fim de dezembro
    assert "R$ 4.579,53" in html
    assert client.get("/extrato?mes=lixo").status_code == 200  # mês inválido = mês atual


def test_voltar_so_para_o_proprio_app(env):
    _, client = env
    login(client)
    r = client.post("/registrar", data={"valor": "1", "voltar": "//site-malicioso.com"},
                    follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == "/"
