"""O token nunca pode aparecer nos logs."""

import logging

from finbot.__main__ import RedactSecretFilter

TOKEN = "123:segredo"


def _format(record: logging.LogRecord) -> str:
    RedactSecretFilter(TOKEN).filter(record)
    return logging.Formatter("%(message)s").format(record)


def test_token_some_da_mensagem_com_argumentos():
    record = logging.LogRecord(
        "x", logging.ERROR, __file__, 1, "url=%s", (f"/bot{TOKEN}/getMe",), None
    )
    assert _format(record) == "url=/bot***/getMe"


def test_token_some_do_traceback():
    try:
        raise RuntimeError(f"The token `{TOKEN}` was rejected")
    except RuntimeError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "falhou", None, sys.exc_info())
    text = _format(record)
    assert TOKEN not in text
    assert "The token `***` was rejected" in text
