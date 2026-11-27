"""Fluxos guiados (cadastros/edições): só um por vez.

Cada fluxo guarda seu rascunho em context.user_data sob uma destas chaves. Antes
de começar um fluxo, `busy` confere se há OUTRO em andamento: dois ao mesmo tempo
fariam as respostas de um caírem no outro.
"""

from __future__ import annotations

FLOW_KEYS = (
    "debt_draft",  # /novadivida
    "debt_edit",  # /editardivida
    "card_draft",  # /novocartao
    "card_edit",  # /editarcartao
    "installment_draft",  # /parcelado
    "refund_draft",  # /estorno
    "salary_draft",  # /salario (cadastro)
    "salary_edit",  # /editarsalario
)

BUSY_TEXT = "Há outro cadastro ou edição em andamento. Termine ou use /cancelar."


def busy(user_data: dict, own_key: str | None = None) -> bool:
    return any(user_data.get(k) for k in FLOW_KEYS if k != own_key)
