"""python -m finbot.web          inicia o app em http://127.0.0.1:WEB_PORT
python -m finbot.web senha    define a senha (grava só o HASH no .env)

O servidor escuta só no próprio PC (127.0.0.1). Para abrir no celular, de
qualquer lugar, o Tailscale faz a ponte com HTTPS (veja o README):
    tailscale serve --bg 8000
"""

from __future__ import annotations

import getpass
import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from finbot.config import ConfigError, load_settings
from finbot.web.app import create_app
from finbot.web.config import load_web_settings
from finbot.web.security import hash_password

LOG_FILE = Path("data") / "finbot-web.log"
ENV_FILE = Path(".env")


def configure_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
        handlers=handlers,
        force=True,
    )


def set_password() -> None:
    first = getpass.getpass("Nova senha do app (não aparece ao digitar): ")
    if len(first) < 8:
        print("Use pelo menos 8 caracteres.")
        sys.exit(1)
    if getpass.getpass("Repita a senha: ") != first:
        print("As senhas não batem.")
        sys.exit(1)
    line = f"WEB_PASSWORD_HASH={hash_password(first)}"
    text = ENV_FILE.read_text("utf-8") if ENV_FILE.exists() else ""
    if re.search(r"^WEB_PASSWORD_HASH=.*$", text, flags=re.M):
        text = re.sub(r"^WEB_PASSWORD_HASH=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n\n# App web (hash da senha; a senha em si não fica salva)\n"
        text += line + "\n"
    ENV_FILE.write_text(text, "utf-8")
    print("✅ Senha salva (só o hash) no .env. Reinicie o app para valer.")


def main() -> None:
    if sys.argv[1:] == ["senha"]:
        set_password()
        return
    configure_logging()
    try:
        settings = load_settings()
        web = load_web_settings()
    except ConfigError as exc:
        logging.error("Erro de configuração: %s", exc)
        sys.exit(1)
    app = create_app(settings, web)
    # log_config=None: usa o nosso logging (arquivo data/finbot-web.log).
    uvicorn.run(app, host="127.0.0.1", port=web.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
