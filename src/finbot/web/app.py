"""Servidor do app web (FastAPI).

Rotas:
  GET  /login, POST /login, POST /sair   entrar e sair
  GET  /                                 Painel
  POST /saldo                            atualiza o saldo da conta (= /extrato inicial)
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
from finbot.config import Settings
from finbot.money import AmountError
from finbot.services import cash
from finbot.web import views
from finbot.web.config import WebSettings
from finbot.web.security import LoginThrottle, secret_key, verify_password

logger = logging.getLogger(__name__)

_HERE = Path(str(resources.files("finbot.web")))
SESSION_DAYS = 30


class LoginRequired(Exception):
    pass


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

    @app.get("/api/saldo-diario", dependencies=[Depends(require_login)])
    def api_daily(conn: sqlite3.Connection = Depends(get_conn)):
        return views.daily_series(conn, today(), settings.timezone, settings.extra_holidays)

    return app
