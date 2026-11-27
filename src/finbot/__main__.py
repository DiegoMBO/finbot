"""Ponto de entrada. Rode com:  python -m finbot"""

from __future__ import annotations

import logging
import sys

from telegram import Update
from telegram.error import InvalidToken, NetworkError

from finbot.bot.app import build_application
from finbot.config import ConfigError, load_settings


def configure_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
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


def main() -> None:
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        # Erro de configuração: mensagem curta e saída com código 1, sem traceback.
        print(f"Erro de configuração: {exc}", file=sys.stderr)
        sys.exit(1)

    # Filtros no HANDLER (e não no logger) valem para as mensagens de todos os
    # módulos, inclusive as da biblioteca, que sobem até o logger raiz.
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactSecretFilter(settings.telegram_token))

    app = build_application(settings)
    logging.getLogger(__name__).info("Conectando ao Telegram... (Ctrl+C para parar)")

    try:
        # Long polling: o bot pergunta ao Telegram de tempos em tempos se há
        # mensagens novas. Não precisa de servidor público nem HTTPS, ideal para
        # rodar no seu computador. (A alternativa, webhook, fica para o deploy.)
        # bootstrap_retries: se a conexão inicial falhar (rede instável), tenta
        # de novo até 5 vezes em vez de desistir na primeira.
        app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=5)
    except InvalidToken:
        print("Erro: o Telegram recusou o TELEGRAM_TOKEN. Confira o .env.", file=sys.stderr)
        sys.exit(1)
    except NetworkError as exc:
        # TimedOut é um tipo de NetworkError. Depois das novas tentativas, não
        # adianta mostrar 100 linhas de traceback: o problema é a conexão.
        print(
            f"Erro de rede ao falar com o Telegram ({exc}). "
            "Verifique sua internet/firewall/VPN e tente de novo.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
