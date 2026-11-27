"""Servidor do app web (FastAPI).

Rotas:
  GET  /login, POST /login, POST /sair   entrar e sair
  GET  /                                 Painel
  POST /saldo                            atualiza o saldo da conta (= /extrato inicial)
  GET  /extrato?mes=AAAA-MM              entra − dívidas − faturas − gastos (= /extrato)
  GET  /gastos?mes=AAAA-MM               gastos do mês, gráficos, orçamentos, registros
  POST /registrar                        gasto, entrada ou compra no cartão
  POST /desfazer                         desfaz o último registro (= /desfazer)
  GET  /dividas, /dividas/{id}           dívidas, simulador e detalhe com gráfico
  POST /pagar                            pagamento de dívida (= /pagar)
  GET  /api/simular?extra=CENTAVOS       simulador de todas as dívidas (JSON)
  GET  /api/dividas/{id}/simular         simulador de uma dívida (JSON)
  GET  /api/saldo-diario                 dados do gráfico (JSON)

"Handlers finos", como no bot: cada rota só lê o pedido, chama services/ ou
views.py e devolve a tela. As contas ficam em um lugar só.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from finbot import db
from finbot.bot.cash import parse_signed_amount
from finbot.bot.payments import format_payment
from finbot.config import Settings
from finbot.money import AmountError, parse_amount
from finbot.services import cash, debts, undo
from finbot.web import debts_view, entries, views
from finbot.web.config import WebSettings
from finbot.web.security import LoginThrottle, secret_key, verify_password

logger = logging.getLogger(__name__)

_HERE = Path(str(resources.files("finbot.web")))
SESSION_DAYS = 30


class LoginRequired(Exception):
    pass


def _safe_back(path: str) -> str:
    """Só volta para páginas do próprio app (evita redirecionar para outro site)."""
    if path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/"


def create_app(settings: Settings, web: WebSettings, clock=None) -> FastAPI:
    """`clock`: função que devolve "agora" (datetime com fuso); os testes passam uma fixa."""
    now_fn = clock or (lambda: datetime.now(UTC))
    app = FastAPI(title="finbot", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key(web.secret_path),
        max_age=SESSION_DAYS * 24 * 3600,
        same_site="lax",  # o cookie não vai em formulários enviados por outros sites
        https_only=web.https_only,
    )
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=_HERE / "templates")
    templates.env.globals["brl"] = views.brl
    throttle = LoginThrottle()

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(settings.database_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    def require_login(request: Request) -> None:
        if not request.session.get("ok"):
            raise LoginRequired

    def today():
        return now_fn().astimezone(settings.timezone).date()

    @app.exception_handler(LoginRequired)
    async def _to_login(request: Request, _exc: LoginRequired) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"erro": "faça login"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"  # dados financeiros: sem cache
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, senha: str = Form(...)):
        locked = throttle.seconds_locked()
        if locked:
            error = f"Muitas tentativas. Espere {locked // 60 + 1} min."
        elif verify_password(senha, web.password_hash):
            throttle.success()
            request.session.clear()
            request.session["ok"] = True
            return RedirectResponse("/", status_code=303)
        else:
            throttle.fail()
            logger.warning("Senha errada no login do app web")
            error = "Senha errada."
        return templates.TemplateResponse(request, "login.html", {"error": error}, status_code=401)

    @app.post("/sair")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        data = views.dashboard(conn, today(), settings.timezone, settings.extra_holidays)
        flash = request.session.pop("flash", None)
        return templates.TemplateResponse(
            request,
            "painel.html",
            {**data, "flash": flash, "page": "painel", "today_tz": settings.timezone},
        )

    @app.post("/saldo", dependencies=[Depends(require_login)])
    def set_balance(
        request: Request, valor: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)
    ):
        try:
            amount = parse_signed_amount(valor)
        except AmountError:
            request.session["flash"] = f"Não entendi o valor {valor!r}. Ex.: 350 ou -120,50"
            return RedirectResponse("/", status_code=303)
        cash.set_anchor(conn, amount, now_fn())
        request.session["flash"] = f"Saldo da conta atualizado: {views.brl(amount)}"
        return RedirectResponse("/", status_code=303)

    @app.get("/gastos", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    def spending_page(
        request: Request, mes: str | None = None, conn: sqlite3.Connection = Depends(get_conn)
    ):
        y, m = views.parse_month_key(mes, today())
        data = views.spending(conn, y, m, today(), settings.timezone)
        flash = request.session.pop("flash", None)
        return templates.TemplateResponse(
            request, "gastos.html", {**data, "flash": flash, "page": "gastos"}
        )

    @app.get("/extrato", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    def statement_page(
        request: Request, mes: str | None = None, conn: sqlite3.Connection = Depends(get_conn)
    ):
        y, m = views.parse_month_key(mes, today())
        data = views.statement_page(conn, y, m, today(), settings.timezone, settings.extra_holidays)
        return templates.TemplateResponse(request, "extrato.html", {**data, "page": "extrato"})

    @app.post("/registrar", dependencies=[Depends(require_login)])
    def register(
        request: Request,
        tipo: str = Form("expense"),
        valor: str = Form(""),
        descricao: str = Form(""),
        categoria: str = Form(""),
        cartao: str = Form(""),
        parcelas: str = Form("1"),
        data: str = Form(""),
        voltar: str = Form("/gastos"),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        form = entries.EntryForm(tipo, valor, descricao, categoria, cartao, parcelas, data)
        try:
            request.session["flash"] = entries.register(conn, form, now_fn(), settings.timezone)
        except entries.EntryError as exc:
            request.session["flash"] = f"Não registrei: {exc}."
        return RedirectResponse(_safe_back(voltar), status_code=303)

    @app.post("/desfazer", dependencies=[Depends(require_login)])
    def undo_last(
        request: Request,
        voltar: str = Form("/gastos"),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        # A mesma regra do /desfazer do bot (services/undo.py).
        request.session["flash"] = undo.undo_last(conn, settings.timezone)
        return RedirectResponse(_safe_back(voltar), status_code=303)

    @app.get("/dividas", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    def debts_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        data = debts_view.debts_page(conn, today())
        flash = request.session.pop("flash", None)
        return templates.TemplateResponse(
            request, "dividas.html", {**data, "flash": flash, "page": "dividas"}
        )

    @app.get(
        "/dividas/{debt_id}", response_class=HTMLResponse, dependencies=[Depends(require_login)]
    )
    def debt_page(request: Request, debt_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        data = debts_view.debt_detail(conn, debt_id, today(), settings.timezone)
        if data is None:
            return RedirectResponse("/dividas", status_code=303)
        flash = request.session.pop("flash", None)
        return templates.TemplateResponse(
            request, "divida.html", {**data, "flash": flash, "page": "dividas"}
        )

    @app.post("/pagar", dependencies=[Depends(require_login)])
    def pay_debt(
        request: Request,
        divida: int = Form(...),
        valor: str = Form(""),
        voltar: str = Form("/dividas"),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        debt = next((d for d in debts.list_active(conn) if d.id == divida), None)
        try:
            amount = parse_amount(valor)
        except AmountError:
            amount = 0
        if debt is None:
            request.session["flash"] = "Essa dívida não está mais ativa. Nada foi registrado."
        elif amount <= 0:
            request.session["flash"] = "Não registrei: valor inválido (ex.: 150 ou 99,90)."
        else:
            # O mesmo do bot: juros do mês, saldo, gasto em "dívidas" e /desfazer.
            result = debts.pay(conn, debt, amount, now_fn(), settings.timezone)
            request.session["flash"] = format_payment(result, today())
        return RedirectResponse(_safe_back(voltar), status_code=303)

    @app.get("/api/simular", dependencies=[Depends(require_login)])
    def api_simulate(extra: int = 0, conn: sqlite3.Connection = Depends(get_conn)):
        return debts_view.simulate(conn, today(), extra)

    @app.get("/api/dividas/{debt_id}/simular", dependencies=[Depends(require_login)])
    def api_simulate_one(
        debt_id: int, extra: int = 0, conn: sqlite3.Connection = Depends(get_conn)
    ):
        if debt_id not in {d.id for d in debts.list_all(conn)}:
            return JSONResponse({"erro": "dívida não encontrada"}, status_code=404)
        return debts_view.simulate_one(conn, debt_id, today(), extra)

    @app.get("/api/saldo-diario", dependencies=[Depends(require_login)])
    def api_daily(conn: sqlite3.Connection = Depends(get_conn)):
        return views.daily_series(conn, today(), settings.timezone, settings.extra_holidays)

    return app
