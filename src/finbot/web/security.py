"""Login do app web: senha com hash, chave da sessão e limite de tentativas.

Decisões:
- A senha NUNCA é guardada: o .env guarda só o hash (scrypt, da biblioteca padrão
  do Python, com "sal" aleatório). Mesmo quem ler o .env não descobre a senha.
- A sessão é um cookie assinado com uma chave secreta guardada em
  data/web_secret.key (criada sozinha; fora do git, porque data/ é ignorada).
- Depois de 5 senhas erradas, o login fica bloqueado por 5 minutos.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path

# Parâmetros do scrypt: custo alto o bastante para atrapalhar quem tenta adivinhar.
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        name, n, r, p, salt, digest = stored.split("$")
        if name != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p)
        )
    except ValueError:
        return False
    # compare_digest: compara em tempo constante (não "vaza" quantos bytes batem).
    return hmac.compare_digest(candidate.hex(), digest)


def secret_key(path: Path) -> str:
    """Chave que assina o cookie de sessão. Criada na 1ª vez e reaproveitada."""
    if path.exists():
        return path.read_text("utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    path.write_text(key, "utf-8")
    return key


class LoginThrottle:
    """Conta senhas erradas; bloqueia por `lock_seconds` depois de `max_failures`."""

    def __init__(self, max_failures: int = 5, lock_seconds: int = 300, clock=time.monotonic):
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self.clock = clock
        self.failures = 0
        self.locked_until = 0.0

    def seconds_locked(self) -> int:
        return max(int(self.locked_until - self.clock()), 0)

    def fail(self) -> None:
        self.failures += 1
        if self.failures >= self.max_failures:
            self.locked_until = self.clock() + self.lock_seconds
            self.failures = 0

    def success(self) -> None:
        self.failures = 0
