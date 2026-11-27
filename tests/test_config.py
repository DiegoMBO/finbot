"""Testes da leitura de configuração."""

from pathlib import Path

import pytest

from finbot.config import ConfigError, load_settings

VALID_ENV = {"TELEGRAM_TOKEN": "123:abc", "ALLOWED_CHAT_ID": "987654"}


def test_valores_minimos_usam_padroes():
    settings = load_settings(VALID_ENV)
    assert settings.allowed_chat_id == 987654
    assert settings.database_path == Path("data/finbot.db")
    assert settings.timezone.key == "America/Sao_Paulo"


def test_chat_id_negativo_e_aceito():
    settings = load_settings({**VALID_ENV, "ALLOWED_CHAT_ID": "-100123"})
    assert settings.allowed_chat_id == -100123


@pytest.mark.parametrize("missing", ["TELEGRAM_TOKEN", "ALLOWED_CHAT_ID"])
def test_falta_variavel_obrigatoria(missing):
    env = {k: v for k, v in VALID_ENV.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_settings(env)


def test_chat_id_nao_numerico():
    with pytest.raises(ConfigError, match="inteiro"):
        load_settings({**VALID_ENV, "ALLOWED_CHAT_ID": "meu-id"})


def test_fuso_invalido():
    with pytest.raises(ConfigError, match="Fuso"):
        load_settings({**VALID_ENV, "TIMEZONE": "Marte/Olympus"})
