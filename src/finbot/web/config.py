"""Configuração do app web (variáveis WEB_* do .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from finbot.config import ConfigError


@dataclass(frozen=True)
class WebSettings:
    password_hash: str  # gerado por: python -m finbot.web senha
    port: int = 8000
    secret_path: Path = Path("data") / "web_secret.key"
    # True: o cookie só vai por HTTPS (o Tailscale serve dá HTTPS). Desligue só
    # para testar no próprio PC por http://127.0.0.1 em navegador antigo.
    https_only: bool = True


def load_web_settings(env: dict[str, str] | None = None) -> WebSettings:
    if env is None:
        load_dotenv()
        env = dict(os.environ)
    password_hash = env.get("WEB_PASSWORD_HASH", "").strip()
    if not password_hash:
        raise ConfigError(
            "WEB_PASSWORD_HASH não definido. Crie a senha com: python -m finbot.web senha"
        )
    raw_port = env.get("WEB_PORT", "8000").strip() or "8000"
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ConfigError(f"WEB_PORT deve ser um número, recebi {raw_port!r}.") from exc
    https_only = env.get("WEB_HTTPS_ONLY", "1").strip().lower() not in {"0", "false", "nao"}
    return WebSettings(password_hash=password_hash, port=port, https_only=https_only)
