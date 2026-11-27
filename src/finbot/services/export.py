"""Geração do CSV do /exportar.

Decisões (pensando em abrir no Excel / Google Planilhas em português):
- separador ";": no Brasil a vírgula é o separador decimal, então o Excel
  em pt-BR espera ";" entre as colunas;
- valor com vírgula ("12,50") e data como dd/mm/aaaa, no fuso do usuário;
- codificação "utf-8-sig": o BOM no início faz o Excel reconhecer UTF-8 e
  mostrar "alimentação" em vez de "alimentaÃ§Ã£o";
- proteção contra "CSV injection": um texto começando com = + - @ seria
  interpretado como FÓRMULA pela planilha. Prefixamos com ' para virar texto.
"""

from __future__ import annotations

import csv
import io
from zoneinfo import ZoneInfo

from finbot.services.transactions import Transaction

HEADER = ["data", "hora", "tipo", "valor", "categoria", "descricao"]
KIND_LABELS = {"expense": "gasto", "income": "entrada"}
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _safe_text(value: str) -> str:
    return "'" + value if value.startswith(_FORMULA_START) else value


def _amount(cents: int) -> str:
    reais, centavos = divmod(cents, 100)
    return f"{reais},{centavos:02d}"  # sem separador de milhar: planilhas leem melhor


def to_csv(items: list[Transaction], tz: ZoneInfo) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(HEADER)
    for t in items:
        local = t.created_at.astimezone(tz)
        writer.writerow(
            [
                local.strftime("%d/%m/%Y"),
                local.strftime("%H:%M"),
                KIND_LABELS[t.kind],
                _amount(t.amount_cents),
                _safe_text(t.category),
                _safe_text(t.description),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")
