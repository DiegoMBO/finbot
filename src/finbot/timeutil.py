"""Conversão de datas para o formato guardado no banco.

Tudo é guardado em UTC, no formato fixo "AAAA-MM-DDTHH:MM:SSZ". Com formato
fixo, comparar os textos equivale a comparar as datas (ORDER BY, >=, <).
"""

from __future__ import annotations

from datetime import UTC, datetime

DB_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def to_db(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("datetime sem fuso horário; use datetime.now(UTC)")
    return moment.astimezone(UTC).strftime(DB_FORMAT)


def from_db(text: str) -> datetime:
    return datetime.strptime(text, DB_FORMAT).replace(tzinfo=UTC)
