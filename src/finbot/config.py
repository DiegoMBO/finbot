"""Leitura e validação da configuração (variáveis de ambiente / arquivo .env).

Decisão: toda a configuração passa por aqui e vira um objeto `Settings` imutável.
O resto do código nunca lê `os.environ` diretamente. Vantagens:
- se faltar algo, o bot falha logo ao iniciar, com uma mensagem clara;
- nos testes dá para criar um `Settings` à mão, sem mexer em variáveis de ambiente.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigError(Exception):
    """Configuração ausente ou inválida."""


@dataclass(frozen=True)
class HealthSettings:
    """Regras do /saude. Todas configuráveis no .env (veja .env.example)."""

    fixed_categories: tuple[str, ...] = ("moradia", "saúde", "educação")
    ok_max_pct: int = 30  # comprometimento da renda até aqui: saudável
    warn_max_pct: int = 40  # até aqui: atenção; acima: alerta
    min_growth_pct: int = 20  # sugestão só se a categoria cresceu pelo menos isso...
    min_growth_cents: int = 5_000  # ...e pelo menos este valor (R$ 50,00)
    share_pct: int = 50  # quanto do aumento a sugestão manda para a dívida


@dataclass(frozen=True)  # frozen=True: ninguém altera a configuração depois de criada
class Settings:
    telegram_token: str
    allowed_chat_id: int
    database_path: Path
    timezone: ZoneInfo
    health: HealthSettings = HealthSettings()
    reminder_hour: int = 9  # hora do dia (no seu fuso) em que os lembretes saem
    reminder_days_before: int = 3  # quantos dias antes do vencimento avisar
    # Feriados estaduais/municipais (dia, mês), além dos nacionais: entram no DSR
    # das horas extras e no cálculo do 5º dia útil.
    extra_holidays: tuple[tuple[int, int], ...] = ()


def _int(env: dict[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} deve ser um número inteiro, recebi {raw!r}.") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} deve estar entre {low} e {high}, recebi {value}.")
    return value


def _holidays(env: dict[str, str]) -> tuple[tuple[int, int], ...]:
    """EXTRA_HOLIDAYS=25/01,09/07 -> ((25, 1), (9, 7))."""
    raw = env.get("EXTRA_HOLIDAYS", "").strip()
    found = []
    for part in (p.strip() for p in raw.split(",") if p.strip()):
        try:
            day, month = (int(x) for x in part.split("/"))
            date(2024, month, day)  # 2024: ano bissexto, aceita 29/02
        except ValueError as exc:
            raise ConfigError(f"EXTRA_HOLIDAYS: data inválida {part!r} (use DD/MM).") from exc
        found.append((day, month))
    return tuple(found)


def _health(env: dict[str, str]) -> HealthSettings:
    default = HealthSettings()
    raw_fixed = env.get("FIXED_CATEGORIES", "").strip()
    fixed = (
        tuple(c.strip().lower() for c in raw_fixed.split(",") if c.strip())
        if raw_fixed
        else default.fixed_categories
    )
    ok = _int(env, "HEALTH_OK_MAX", default.ok_max_pct, 1, 100)
    warn = _int(env, "HEALTH_WARN_MAX", default.warn_max_pct, 1, 100)
    if ok >= warn:
        raise ConfigError(f"HEALTH_OK_MAX ({ok}) precisa ser menor que HEALTH_WARN_MAX ({warn}).")
    return HealthSettings(
        fixed_categories=fixed,
        ok_max_pct=ok,
        warn_max_pct=warn,
        min_growth_pct=_int(env, "SUGGEST_MIN_GROWTH_PCT", default.min_growth_pct, 1, 1000),
        min_growth_cents=_int(env, "SUGGEST_MIN_GROWTH_BRL", 50, 1, 1_000_000) * 100,
        share_pct=_int(env, "SUGGEST_SHARE_PCT", default.share_pct, 1, 100),
    )


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Monta o `Settings` a partir de um dicionário ou das variáveis de ambiente.

    O parâmetro `env` existe para os testes: dá para passar um dict qualquer.
    Quando é None, carregamos o arquivo .env e usamos `os.environ`.
    """
    if env is None:
        # Lê o .env da pasta atual, se existir (não sobrescreve variáveis já definidas).
        load_dotenv()
        env = dict(os.environ)

    token = env.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_TOKEN não definido. Copie .env.example para .env e preencha.")

    raw_chat_id = env.get("ALLOWED_CHAT_ID", "").strip()
    if not raw_chat_id:
        raise ConfigError(
            "ALLOWED_CHAT_ID não definido. Veja no README como descobrir seu chat id."
        )
    try:
        # Chat ids podem ser negativos (grupos), por isso int() e não isdigit().
        allowed_chat_id = int(raw_chat_id)
    except ValueError as exc:
        raise ConfigError(
            f"ALLOWED_CHAT_ID deve ser um número inteiro, recebi {raw_chat_id!r}."
        ) from exc

    tz_name = env.get("TIMEZONE", "America/Sao_Paulo").strip()
    try:
        timezone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Fuso horário desconhecido: {tz_name!r}.") from exc

    database_path = Path(env.get("DATABASE_PATH", "data/finbot.db").strip())

    return Settings(
        telegram_token=token,
        allowed_chat_id=allowed_chat_id,
        database_path=database_path,
        timezone=timezone,
        health=_health(env),
        reminder_hour=_int(env, "REMINDER_HOUR", 9, 0, 23),
        reminder_days_before=_int(env, "REMINDER_DAYS_BEFORE", 3, 0, 15),
        extra_holidays=_holidays(env),
    )
