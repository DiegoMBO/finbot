"""Utilidades de texto."""

from __future__ import annotations

import unicodedata


def normalize(word: str) -> str:
    """Minúsculas e sem acentos: "Salário" -> "salario". Usado só para comparar."""
    decomposed = unicodedata.normalize("NFKD", word.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))
