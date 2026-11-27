"""Acesso ao banco SQLite e migrações de esquema.

Decisão: SQLite porque é um arquivo só, vem junto com o Python e sobra para um
bot de uma pessoa. Não precisa instalar nem manter um servidor de banco.

Migrações: o esquema evolui por arquivos numerados em `db/migrations/`
(001_base.sql, 002_dividas.sql, ...). O número da última migração aplicada fica
guardado no próprio banco, em `PRAGMA user_version`. Ao abrir o banco:
1. descobrimos quais migrações ainda não foram aplicadas;
2. se houver alguma e o banco já tiver dados, fazemos um BACKUP antes;
3. aplicamos cada uma dentro de uma transação: ou ela entra inteira, ou nada
   muda (se der erro no meio, o ROLLBACK desfaz tudo).
Regra do projeto: migrações só ADICIONAM (tabelas, índices, colunas). Nunca
apagam nem reescrevem dados existentes.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path

MEMORY = ":memory:"  # banco temporário em memória, usado nos testes

logger = logging.getLogger(__name__)

_MIGRATION_NAME = re.compile(r"(\d{3})_[a-z0-9_]+\.sql")


class MigrationError(Exception):
    """Uma migração falhou; o banco ficou na versão anterior."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    """Lê os arquivos .sql de db/migrations, em ordem de versão."""
    folder = resources.files("finbot.db.migrations")
    found = []
    for entry in folder.iterdir():
        match = _MIGRATION_NAME.fullmatch(entry.name)
        if match:
            found.append(Migration(int(match.group(1)), entry.name, entry.read_text("utf-8")))
    found.sort(key=lambda m: m.version)
    # Versões precisam ser 1, 2, 3...: um buraco indicaria arquivo faltando.
    expected = list(range(1, len(found) + 1))
    if [m.version for m in found] != expected:
        raise MigrationError(f"migrações fora de sequência: {[m.name for m in found]}")
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _has_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()
    return row[0] > 0


def backup(conn: sqlite3.Connection, db_path: Path, version: int) -> Path:
    """Copia o banco para data/backups/finbot-AAAAMMDD-HHMMSS-vN.db.

    Usa a API de backup do SQLite em vez de copiar o arquivo: ela gera uma cópia
    consistente mesmo com o banco aberto.
    """
    folder = db_path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = folder / f"{db_path.stem}-{stamp}-v{version}.db"
    counter = 1
    while target.exists():  # dois backups no mesmo segundo (só acontece em testes)
        counter += 1
        target = folder / f"{db_path.stem}-{stamp}-v{version}-{counter}.db"
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def migrate(
    conn: sqlite3.Connection,
    migrations: list[Migration],
    db_path: Path | None = None,
) -> Path | None:
    """Aplica as migrações pendentes. Devolve o caminho do backup, se houver um."""
    current = schema_version(conn)
    pending = [m for m in migrations if m.version > current]
    if not pending:
        return None

    backup_path = None
    # Banco novo (sem tabelas) não tem o que perder: não precisa de backup.
    if db_path is not None and _has_tables(conn):
        backup_path = backup(conn, db_path, current)
        logger.info("Backup do banco antes da migração: %s", backup_path)

    for m in pending:
        try:
            # executescript confirma qualquer transação aberta e roda o texto como
            # está; por isso o BEGIN/COMMIT explícito. O user_version é gravado na
            # MESMA transação: a versão só muda se a migração entrar inteira.
            conn.executescript(f"BEGIN;\n{m.sql}\nPRAGMA user_version = {m.version};\nCOMMIT;")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.rollback()
            hint = f" Backup: {backup_path}" if backup_path else ""
            raise MigrationError(f"falha na migração {m.name}: {exc}.{hint}") from exc
        logger.info("Migração aplicada: %s", m.name)
    return backup_path


def connect(path: Path | str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Abre (ou cria) o banco em `path` e aplica as migrações pendentes.

    `check_same_thread=False` é para o app web: lá cada requisição abre a sua
    conexão, mas o FastAPI pode usá-la em outra thread do mesmo pedido.
    """
    is_file = str(path) != MEMORY
    if is_file:
        # Cria a pasta "data/" na primeira execução.
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    # timeout: se o outro programa (bot ou app) estiver gravando, espera até 10 s
    # em vez de falhar na hora com "database is locked".
    conn = sqlite3.connect(path, timeout=10, check_same_thread=check_same_thread)
    # Row permite ler colunas pelo nome: row["amount_cents"] em vez de row[2].
    conn.row_factory = sqlite3.Row
    # O SQLite vem com as chaves estrangeiras DESLIGADAS por padrão. Ligadas, ele
    # recusa, por exemplo, um pagamento que aponte para uma dívida inexistente.
    conn.execute("PRAGMA foreign_keys = ON")
    if is_file:
        # WAL ("write-ahead log"): o bot e o app web usam o MESMO arquivo ao mesmo
        # tempo; quem lê não bloqueia quem grava. A configuração fica salva no
        # arquivo (cria finbot.db-wal e finbot.db-shm ao lado; não apague).
        conn.execute("PRAGMA journal_mode = WAL")

    try:
        migrate(conn, load_migrations(), Path(path) if is_file else None)
    except Exception:
        conn.close()
        raise
    return conn
