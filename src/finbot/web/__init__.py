"""App web do finbot: as mesmas contas do bot, em telas com gráficos.

Rode com:  python -m finbot.web        (servidor em http://127.0.0.1:8000)
Senha:     python -m finbot.web senha  (grava WEB_PASSWORD_HASH no .env)

O app e o bot usam o MESMO banco (data/finbot.db, em modo WAL) e as mesmas
funções de services/: o que você registra num aparece no outro.
"""
