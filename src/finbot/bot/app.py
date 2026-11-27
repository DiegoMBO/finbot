"""Montagem da aplicação do Telegram: cria o bot e registra os handlers."""

from __future__ import annotations

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from finbot import db
from finbot.bot import (
    cards,
    chart,
    debt_edit,
    debts,
    handlers,
    health,
    payments,
    reminders,
    strategy,
)
from finbot.bot.security import build_guard
from finbot.config import Settings


async def _close_db(app: Application) -> None:
    """Chamado pela biblioteca quando o bot desliga (Ctrl+C)."""
    app.bot_data["db"].close()


def build_application(settings: Settings) -> Application:
    """Cria a `Application` já com todos os handlers registrados."""
    app = (
        Application.builder()
        .token(settings.telegram_token)
        # O padrão da biblioteca é 5 s; numa conexão lenta isso estoura à toa
        # (aconteceu na primeira execução). 10 s para conectar, 20 s para ler.
        .connect_timeout(10)
        .read_timeout(20)
        .post_shutdown(_close_db)
        .build()
    )

    # bot_data é um dicionário compartilhado que todos os handlers acessam via
    # context.bot_data. Guardamos as configurações para não usar variáveis globais.
    app.bot_data["settings"] = settings
    # Uma única conexão SQLite para o bot inteiro. É seguro porque, por padrão,
    # a biblioteca processa uma atualização por vez, na mesma thread.
    app.bot_data["db"] = db.connect(settings.database_path)

    # Grupo -1: roda antes de tudo e barra chats não autorizados (veja security.py).
    app.add_handler(build_guard(settings.allowed_chat_id), group=-1)

    # Grupo 0 (padrão): os handlers de verdade. Dentro de um grupo, vale o
    # PRIMEIRO handler que aceitar a atualização, então a ORDEM importa.
    # O cadastro guiado vem primeiro: durante o cadastro, "Nubank" é a resposta
    # de uma pergunta, e não pode cair no handler de texto livre lá embaixo.
    app.add_handler(debts.build_conversation())
    app.add_handler(debt_edit.build_conversation())  # /editardivida: mesma razão
    app.add_handler(CommandHandler(["start", "ajuda"], handlers.start))
    app.add_handler(CommandHandler("hoje", handlers.today))
    app.add_handler(CommandHandler("mes", handlers.month))
    app.add_handler(CommandHandler("desfazer", handlers.undo))
    app.add_handler(CommandHandler("orcamento", handlers.budget))
    app.add_handler(CommandHandler("exportar", handlers.export_csv))
    app.add_handler(CommandHandler("dividas", debts.list_debts))
    app.add_handler(CommandHandler("divida", debts.show_debt))
    app.add_handler(CommandHandler("pagar", payments.pay_command))
    app.add_handler(CommandHandler("saldo", payments.balance_command))
    app.add_handler(CommandHandler("fatura", cards.statement_command))
    app.add_handler(CommandHandler("simular", strategy.simulate_command))
    app.add_handler(CommandHandler("estrategia", strategy.strategy_command))
    app.add_handler(CommandHandler("saude", health.health_command))
    app.add_handler(CommandHandler("grafico", chart.chart_command))
    app.add_handler(CommandHandler("lembretes", reminders.reminders_command))
    app.add_handler(CallbackQueryHandler(handlers.payment_choice, pattern=f"^{payments.CB}"))
    app.add_handler(CommandHandler("cancelar", debts.cancel))  # fora de um cadastro
    # Qualquer texto que não seja comando é tratado como um registro.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.register))
    # Botões de cadastros que já terminaram.
    app.add_handler(
        CallbackQueryHandler(debts.expired_button, pattern=f"^({debts.CB}|{debt_edit.CB})")
    )

    # Tarefas agendadas: lembretes de vencimento (todo dia e logo ao ligar).
    reminders.schedule(app, settings)

    return app
