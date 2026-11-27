#!/usr/bin/env bash
# Backup do banco do finbot: data/backups/diario-AAAAMMDD-HHMMSS.db
# Mantém os 14 mais recentes. Seguro com o bot rodando: usa a API de backup do
# SQLite (cópia consistente), e não um "cp" do arquivo aberto.
#
#     bash deploy/backup.sh
#
# O instalar.sh agenda este script todo dia às 03:30 (timer finbot-backup).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
KEEP=14

# O caminho do banco vem do mesmo lugar que o bot usa (.env / DATABASE_PATH).
DB_PATH="$(.venv/bin/python -c 'from finbot.config import load_settings; print(load_settings().database_path)')"
if [ ! -f "$DB_PATH" ]; then
    echo "Banco não encontrado em $DB_PATH; nada a copiar."
    exit 0
fi

BACKUP_DIR="$(dirname "$DB_PATH")/backups"
mkdir -p "$BACKUP_DIR"
TARGET="$BACKUP_DIR/diario-$(date +%Y%m%d-%H%M%S).db"

.venv/bin/python - "$DB_PATH" "$TARGET" <<'PY'
import sqlite3
import sys

source, target = sys.argv[1], sys.argv[2]
src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
dst = sqlite3.connect(target)
with dst:
    src.backup(dst)
dst.close()
src.close()
PY

echo "Backup: $TARGET"

# Apaga os backups diários além dos $KEEP mais recentes (os das migrações ficam).
ls -1t "$BACKUP_DIR"/diario-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --
