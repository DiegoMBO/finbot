#!/usr/bin/env bash
# Instala o finbot como serviço num servidor Linux (Debian/Ubuntu), por exemplo
# a VM e2-micro gratuita do Google Cloud. Rode DE DENTRO da pasta do projeto:
#
#     bash deploy/instalar.sh
#
# Pode rodar de novo quantas vezes quiser (é idempotente): ele só refaz o que falta.
# O que ele faz:
#   1. instala o Python (venv) pelo apt;
#   2. cria o ambiente virtual .venv e instala o bot;
#   3. confere o .env (cria a partir do .env.example se não existir e PARA);
#   4. instala o serviço systemd "finbot" (liga sozinho no boot e religa se cair);
#   5. instala o backup diário do banco (timer systemd "finbot-backup").
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
cd "$PROJECT_DIR"

echo "==> Projeto: $PROJECT_DIR (usuário: $RUN_USER)"

if [ "$RUN_USER" = "root" ]; then
    echo "Rode como seu usuário normal (sem sudo); o script pede sudo só quando precisa."
    exit 1
fi

echo "==> 1/5 Pacotes do sistema"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ necessário"'

echo "==> 2/5 Ambiente virtual e dependências"
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .

echo "==> 3/5 Configuração (.env)"
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo
    echo "Criei o .env a partir do exemplo. Preencha TELEGRAM_TOKEN e ALLOWED_CHAT_ID:"
    echo "    nano .env"
    echo "e rode este script de novo."
    exit 1
fi
chmod 600 .env  # só o seu usuário lê o token
# Falha cedo, com mensagem clara, se o .env estiver incompleto.
.venv/bin/python -c "from finbot.config import load_settings; load_settings()"

echo "==> 4/5 Serviço do bot (systemd)"
sudo tee /etc/systemd/system/finbot.service >/dev/null <<EOF
[Unit]
Description=finbot (bot de finanças no Telegram)
Wants=network-online.target
After=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python -m finbot
Restart=always
RestartSec=10
# Logs acentuados certinhos no journal.
Environment=PYTHONIOENCODING=utf-8

[Install]
WantedBy=multi-user.target
EOF

echo "==> 5/5 Backup diário do banco (systemd timer, 03:30)"
sudo tee /etc/systemd/system/finbot-backup.service >/dev/null <<EOF
[Unit]
Description=Backup diário do banco do finbot

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=/usr/bin/env bash $PROJECT_DIR/deploy/backup.sh
EOF
sudo tee /etc/systemd/system/finbot-backup.timer >/dev/null <<EOF
[Unit]
Description=Agenda o backup diário do finbot

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now finbot-backup.timer
sudo systemctl enable finbot.service
sudo systemctl restart finbot.service

sleep 5
if systemctl is-active --quiet finbot.service; then
    echo
    echo "✅ finbot rodando. Comandos úteis:"
    echo "   journalctl -u finbot -f        # log ao vivo (Ctrl+C sai do log; o bot continua)"
    echo "   sudo systemctl restart finbot  # reiniciar"
    echo "   bash deploy/atualizar.sh       # depois de um git pull"
    echo "   bash deploy/backup.sh          # backup agora"
    echo
    echo "⚠️  Lembre de PARAR o bot no seu computador (duas cópias = erro Conflict)."
else
    echo "❌ O serviço não subiu. Veja o motivo com: journalctl -u finbot -n 50"
    exit 1
fi
