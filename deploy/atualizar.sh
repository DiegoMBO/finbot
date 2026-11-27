#!/usr/bin/env bash
# Atualiza o finbot no servidor com a versão mais nova do git:
#
#     bash deploy/atualizar.sh
#
# 1. backup do banco (antes de qualquer mudança);
# 2. git pull;
# 3. reinstala as dependências (caso o pyproject.toml tenha mudado);
# 4. reinicia o serviço. Migrações novas do banco rodam sozinhas ao iniciar,
#    e o bot faz o próprio backup antes de migrar.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "==> Backup"
bash deploy/backup.sh

echo "==> git pull"
git pull --ff-only

echo "==> Dependências"
.venv/bin/pip install -q -e .

echo "==> Reiniciando"
sudo systemctl restart finbot.service
sleep 5
if systemctl is-active --quiet finbot.service; then
    echo "✅ Atualizado e rodando. Log: journalctl -u finbot -f"
else
    echo "❌ O serviço não subiu. Veja: journalctl -u finbot -n 50"
    exit 1
fi
