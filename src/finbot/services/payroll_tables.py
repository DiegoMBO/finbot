"""Tabelas de INSS e IRRF por ano. ATUALIZE TODO ANO (normalmente em janeiro).

Valores em centavos; alíquotas em pontos-base (7,5% = 750).

2026
- INSS: Portaria Interministerial MPS/MF nº 13, de 9/1/2026 (teto R$ 8.475,55).
- IRRF: tabela progressiva mensal da Lei 15.191/2025, com a redução da
  Lei 15.270/2025 (isenção efetiva até R$ 5.000; redução decrescente até R$ 7.350).
Para um ano novo: copie o bloco do ano anterior, troque os números e cite a fonte.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bracket:
    up_to_cents: int | None  # None = sem limite (última faixa)
    rate_bp: int
    deduction_cents: int = 0  # "parcela a deduzir" (só no IRRF)


@dataclass(frozen=True)
class TaxTables:
    year: int
    inss: tuple[Bracket, ...]  # progressivo; a última faixa termina no teto
    irrf: tuple[Bracket, ...]
    dependent_deduction_cents: int  # por dependente
    simplified_deduction_cents: int  # desconto simplificado mensal
    # Redução da Lei 15.270/2025 (a partir de 2026):
    reduction_full_up_to_cents: int  # até aqui, o IR é zerado
    reduction_until_cents: int  # acima daqui, não há redução
    reduction_constant_cents: int  # redução = constante − fator × rendimento
    reduction_factor_millionths: int  # 0,133145 = 133145


TABLES: dict[int, TaxTables] = {
    2026: TaxTables(
        year=2026,
        inss=(
            Bracket(162_100, 750),
            Bracket(290_284, 900),
            Bracket(435_427, 1_200),
            Bracket(847_555, 1_400),  # teto
        ),
        irrf=(
            Bracket(242_880, 0, 0),
            Bracket(282_665, 750, 18_216),
            Bracket(375_105, 1_500, 39_416),
            Bracket(466_468, 2_250, 67_549),
            Bracket(None, 2_750, 90_873),
        ),
        dependent_deduction_cents=18_959,
        simplified_deduction_cents=60_720,
        reduction_full_up_to_cents=500_000,
        reduction_until_cents=735_000,
        reduction_constant_cents=97_862,
        reduction_factor_millionths=133_145,
    ),
}


def tables_for(year: int) -> TaxTables:
    """A tabela do ano; se ainda não houver, a do ano mais recente cadastrado."""
    if year in TABLES:
        return TABLES[year]
    older = [y for y in TABLES if y < year]
    return TABLES[max(older)] if older else TABLES[min(TABLES)]
