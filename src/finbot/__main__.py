"""Ponto de entrada. Rode com:  python -m finbot

Sem janela (pythonw.exe, usado pelo Agendador de Tarefas do Windows) não existe
terminal: o log vai só para o arquivo data/finbot.log.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telegram import Update
from telegram.error import InvalidToken, NetworkError

from finbot.bot.app import build_application
from finbot.config import ConfigError, load_settings

LOG_FILE = Path("data") / "finbot.log"  # relativo à pasta do projeto
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

logger = logging.getLogger(__name__)


def has_console() -> bool:
    """False quando roda com pythonw.exe (sem janela): sys.stderr é None."""
    return sys.stderr is not None


def configure_logging() -> None:
    handlers: list[logging.Handler] = []
    if has_console():
        handlers.append(logging.StreamHandler())
    # Arquivo sempre: até 3 arquivos de 1 MB (finbot.log, .1, .2); o mais velho some.
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handlers.append(
        RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    )
    logging.basicConfig(format=LOG_FORMAT, level=logging.INFO, handlers=handlers, force=True)
    # IMPORTANTE: o httpx (cliente HTTP usado pela biblioteca) registra em nível
    # INFO a URL de cada requisição, e a URL da API do Telegram contém o TOKEN.
    # Subimos o nível para WARNING para o token não aparecer no terminal/logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # O agendador dos lembretes registra cada execução em INFO: ruído. Erros continuam.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


class RedactSecretFilter(logging.Filter):
    """Troca um segredo (o token) por *** em qualquer linha de log.

    Segunda camada de proteção: mesmo silenciando o httpx, a biblioteca do
    Telegram inclui o token em algumas mensagens de erro (ex.: token recusado).
    """

    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.secret in message:
            record.msg, record.args = message.replace(self.secret, "***"), None
        if record.exc_info:
            # O traceback também pode conter o token; formatamos agora, já limpo.
            # (O Formatter reaproveita exc_text em vez de formatar de novo.)
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = text.replace(self.secret, "***")
        return True  # True = a linha continua sendo registrada


def fail(message: str) -> None:
    """Erro fatal: mensagem curta (no terminal e no log) e saída com código 1."""
    logger.error(message)
    if has_console():
        print(message, file=sys.stderr)
    sys.exit(1)


def main() -> None:
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        fail(f"Erro de configuração: {exc}")

    # Filtros no HANDLER (e não no logger) valem para as mensagens de todos os
    # módulos, inclusive as da biblioteca, que sobem até o logger raiz.
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactSecretFilter(settings.telegram_token))

    app = build_application(settings)
    logger.info("Conectando ao Telegram... (Ctrl+C para parar)")

    # Se a conexão inicial falhar (rede instável), tenta de novo em vez de desistir.
    # Sem janela (iniciado pelo Windows ao entrar), a internet pode demorar a subir:
    # aí tenta para sempre. No terminal, 5 tentativas e mostra o erro.
    retries = 5 if has_console() else -1
    try:
        # Long polling: o bot pergunta ao Telegram de tempos em tempos se há
        # mensagens novas. Não precisa de servidor público nem HTTPS, ideal para
        # rodar no seu computador.
        app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=retries)
    except InvalidToken:
        fail("Erro: o Telegram recusou o TELEGRAM_TOKEN. Confira o .env.")
    except NetworkError as exc:
        # TimedOut é um tipo de NetworkError. Depois das novas tentativas, não
        # adianta mostrar 100 linhas de traceback: o problema é a conexão.
        fail(
            f"Erro de rede ao falar com o Telegram ({exc}). "
            "Verifique sua internet/firewall/VPN e tente de novo."
        )


if __name__ == "__main__":
    main()
