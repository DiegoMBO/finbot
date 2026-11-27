"""Adivinha a categoria a partir das palavras da descrição.

Decisão: um dicionário simples de palavras-chave. É previsível, fácil de editar
e não precisa de internet. Quando nada bate, a categoria é "outros"; e você
sempre pode forçar uma categoria escrevendo #categoria na mensagem.
Para adicionar palavras, basta editar EXPENSE_KEYWORDS abaixo (sem acentos).
"""

from __future__ import annotations

from collections.abc import Iterable

from finbot.parsing.text import normalize

DEFAULT_EXPENSE = "outros"
DEFAULT_INCOME = "receita"

EXPENSE_KEYWORDS: dict[str, set[str]] = {
    "alimentação": {
        "mercado",
        "supermercado",
        "padaria",
        "feira",
        "acougue",
        "hortifruti",
        "restaurante",
        "lanche",
        "almoco",
        "janta",
        "jantar",
        "cafe",
        "ifood",
        "pizza",
        "hamburguer",
        "lanchonete",
        "comida",
    },
    "transporte": {
        "uber",
        "99",
        "taxi",
        "onibus",
        "metro",
        "trem",
        "gasolina",
        "combustivel",
        "etanol",
        "estacionamento",
        "pedagio",
        "passagem",
        "bilhete",
    },
    "moradia": {
        "aluguel",
        "condominio",
        "luz",
        "energia",
        "agua",
        "gas",
        "internet",
        "iptu",
    },
    "saúde": {
        "farmacia",
        "remedio",
        "medico",
        "consulta",
        "dentista",
        "exame",
        "plano",
        "academia",
    },
    "lazer": {
        "cinema",
        "netflix",
        "spotify",
        "show",
        "bar",
        "cerveja",
        "jogo",
        "viagem",
        "streaming",
    },
    "educação": {"curso", "livro", "escola", "faculdade", "mensalidade"},
    "compras": {"roupa", "sapato", "presente", "eletronico", "loja"},
}

INCOME_KEYWORDS: dict[str, set[str]] = {
    "salário": {"salario", "pagamento", "holerite"},
    "freela": {"freela", "freelance", "projeto"},
    "investimentos": {"rendimento", "dividendos", "juros"},
}


def _build_index(keywords: dict[str, set[str]]) -> dict[str, str]:
    """Inverte o dicionário: {"mercado": "alimentação", "uber": "transporte", ...}."""
    return {word: category for category, words in keywords.items() for word in words}


_EXPENSE_INDEX = _build_index(EXPENSE_KEYWORDS)
_INCOME_INDEX = _build_index(INCOME_KEYWORDS)


def guess_category(words: Iterable[str], kind: str) -> str:
    """Primeira palavra conhecida decide a categoria; senão, o padrão do tipo."""
    index = _INCOME_INDEX if kind == "income" else _EXPENSE_INDEX
    for word in words:
        category = index.get(normalize(word))
        if category:
            return category
    return DEFAULT_INCOME if kind == "income" else DEFAULT_EXPENSE
