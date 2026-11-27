"""Handlers: funções chamadas pelo Telegram quando chega um comando ou mensagem.

Regra do projeto: handlers são "finos". Eles só traduzem a mensagem do Telegram
para chamadas dos serviços e formatam a resposta. A lógica de verdade fica em
`services/` e `parsing/`, que podem ser testadas sem o Telegram.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from telegram import Message, Update
from telegram.ext import ContextTypes

from finbot.bot import payments
from finbot.bot.texts import format_alert, format_budget_line
from finbot.config import Settings
from finbot.money import format_brl
from finbot.parsing.budget import USAGE as BUDGET_USAGE
from finbot.parsing.budget import BudgetArgsError, parse_budget_args
from finbot.parsing.message import ParseError, parse_message
from finbot.parsing.period import PeriodError, month_label, parse_month
from finbot.services import budgets, debts, export, transactions
from finbot.services.budgets import BudgetStatus
from finbot.services.transactions import Transaction

# Reexportados: os testes (e quem mais precisar) importam daqui.
__all__ = ["format_alert", "format_budget_line"]

HELP_TEXT = (
    "Olá! Eu registro seus gastos e entradas.\n\n"
    "Exemplos de mensagens:\n"
    "• gastei 35 no mercado\n"
    "• 12,50 uber\n"
    "• recebi 3000 salário\n"
    "• 80 presente #família (força a categoria)\n\n"
    "Comandos:\n"
    "/hoje: gastos do dia\n"
    "/mes: resumo do mês (ou /mes 10/2025 para outro mês)\n"
    "/desfazer: apaga o último registro\n"
    "/orcamento: mostra os orçamentos do mês\n"
    "/orcamento categoria valor: define limite mensal\n"
    "/orcamento remover categoria: apaga o limite\n"
    "/exportar: baixa um CSV com tudo (ou /exportar 10/2025 para um mês)\n"
    "/ajuda: mostra esta mensagem\n\n"
    "Dívidas:\n"
    "/novadivida: cadastra uma dívida (perguntas uma a uma)\n"
    "/dividas: resumo de todas as dívidas\n"
    "/divida nome: detalhes e histórico de uma dívida\n"
    "/editardivida nome: muda taxa, parcela, vencimento e outros dados\n"
    "/pagar nome valor: registra um pagamento (ou escreva: paguei 450 do empréstimo nubank)\n"
    "/saldo nome valor: corrige o saldo pelo valor do app do banco\n"
    "/fatura cartão total [mínimo] [vencimento]: registra a fatura do mês\n"
    "/fatura cartão: mostra a fatura atual e o que falta pagar\n"
    "/simular nome extra: quanto você economiza pagando um extra por mês\n"
    "/estrategia [extra]: compara avalanche e bola de neve\n"
    "/saude: indicadores de saúde financeira e sugestões\n"
    "/grafico nome: imagem com a evolução do saldo e a projeção\n"
    "/lembretes: próximos vencimentos (os avisos chegam sozinhos 3 dias antes)\n"
    "/cancelar: sai de um cadastro em andamento"
)


def _db(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    return context.bot_data["db"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


def describe(t: Transaction) -> str:
    """Uma linha legível: "R$ 35,00 em alimentação (mercado)"."""
    text = f"{format_brl(t.amount_cents)} em {t.category}"
    return f"{text} ({t.description})" if t.description else text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde /start e /ajuda com as instruções de uso."""
    # effective_message é a mensagem da atualização, seja ela qual for.
    if update.effective_message:
        await update.effective_message.reply_text(HELP_TEXT)


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Texto livre: pagamento de dívida ("paguei 450 do empréstimo nubank")
    ou, se não for, um gasto/entrada comum ("gastei 35 no mercado")."""
    message = update.effective_message
    if message is None or not message.text:
        return
    if await payments.try_payment(message, context, message.text):
        return
    await record_entry(message, context, message.text)


async def payment_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botões "esse pagamento é de qual dívida?" (veja payments.try_payment)."""
    query = update.callback_query
    pending = context.user_data.pop(payments.PENDING, None)
    if pending is None:
        await query.answer("Esse botão expirou.")
        return
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    choice = query.data.removeprefix(payments.CB)
    if choice == "gasto":
        await record_entry(query.message, context, pending["text"])
        return
    conn = _db(context)
    debt = next((d for d in debts.list_active(conn) if str(d.id) == choice), None)
    if debt is None:
        await query.message.reply_text("Essa dívida não está mais ativa. Nada foi registrado.")
        return
    await payments.record_payment(query.message, context, debt, pending["amount"])


async def record_entry(message: Message, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Registra um gasto ou entrada comum a partir do texto."""
    try:
        entry = parse_message(text)
    except ParseError as exc:
        await message.reply_text(
            f"Não entendi ({exc}). Tente algo como: gastei 35 no mercado\n"
            "/ajuda para mais exemplos."
        )
        return

    conn = _db(context)
    t = transactions.add(conn, entry, now=datetime.now(UTC))
    label = "Entrada registrada" if t.kind == "income" else "Gasto registrado"
    text = f"✅ {label}: {describe(t)}\nErrou? /desfazer"
    alert = budgets.check_alert(conn, t, _settings(context).timezone)
    if alert:
        text += "\n\n" + format_alert(alert)
    await message.reply_text(text)


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hoje: lista os registros do dia (no fuso do usuário) e os totais."""
    message = update.effective_message
    if message is None:
        return
    tz = _settings(context).timezone
    start, end = transactions.day_bounds(datetime.now(tz).date(), tz)
    items = transactions.list_between(_db(context), start, end)
    if not items:
        await message.reply_text("Nenhum registro hoje.")
        return

    lines = ["📅 Hoje:"]
    for t in items:
        hour = t.created_at.astimezone(tz).strftime("%H:%M")
        sign = "+" if t.kind == "income" else "-"
        lines.append(f"{hour} {sign} {describe(t)}")
    summary = transactions.summarize(items)
    lines.append("")
    lines.append(f"Gastos: {format_brl(summary.expense_cents)}")
    if summary.income_cents:
        lines.append(f"Entradas: {format_brl(summary.income_cents)}")
    await message.reply_text("\n".join(lines))


def format_month_report(
    label: str,
    summary: transactions.Summary,
    count: int,
    budget_statuses: list[BudgetStatus] | None = None,
) -> str:
    """Monta o texto do /mes. Separado do handler para dar para testar sem o Telegram."""
    lines = [
        f"📊 Resumo de {label}",
        "",
        f"Entradas: {format_brl(summary.income_cents)}",
        f"Gastos: {format_brl(summary.expense_cents)}",
        f"Saldo: {format_brl(summary.balance_cents)}",
    ]
    if summary.expenses_by_category:
        lines += ["", "Gastos por categoria:"]
        for category, cents in summary.expenses_by_category.items():
            percent = round(100 * cents / summary.expense_cents)
            lines.append(f"• {category}: {format_brl(cents)} ({percent}%)")
    if budget_statuses:
        lines += ["", "Orçamentos:"]
        lines += [format_budget_line(s) for s in budget_statuses]
    lines += ["", f"{count} registro(s) no mês."]
    return "\n".join(lines)


async def month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mes [MM[/AAAA]]: resumo do mês atual ou do mês pedido."""
    message = update.effective_message
    if message is None:
        return
    tz = _settings(context).timezone
    # context.args são as palavras depois do comando: "/mes 10/2025" -> ["10/2025"].
    arg = " ".join(context.args) if context.args else None
    try:
        year, month_number = parse_month(arg, today=datetime.now(tz).date())
    except PeriodError as exc:
        await message.reply_text(f"Não entendi o mês: {exc}.")
        return

    label = month_label(year, month_number)
    start, end = transactions.month_bounds(year, month_number, tz)
    conn = _db(context)
    items = transactions.list_between(conn, start, end)
    if not items:
        await message.reply_text(f"Nenhum registro em {label}.")
        return
    statuses = budgets.statuses(budgets.list_budgets(conn), items)
    report = format_month_report(label, transactions.summarize(items), len(items), statuses)
    await message.reply_text(report)


async def budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/orcamento [remover] [categoria valor]: lista, define ou apaga limites mensais."""
    message = update.effective_message
    if message is None:
        return
    try:
        command = parse_budget_args(list(context.args or []))
    except BudgetArgsError as exc:
        await message.reply_text(f"Não entendi: {exc}.\n\n{BUDGET_USAGE}")
        return

    conn = _db(context)
    if command.action == "set":
        b = budgets.set_budget(conn, command.category, command.limit_cents)
        await message.reply_text(
            f"✅ Orçamento de {b.category}: {format_brl(b.limit_cents)} por mês."
        )
    elif command.action == "remove":
        removed = budgets.remove_budget(conn, command.category)
        if removed:
            await message.reply_text(f"🗑️ Orçamento de {removed.category} apagado.")
        else:
            await message.reply_text(f"Não há orçamento para {command.category}.")
    else:
        tz = _settings(context).timezone
        statuses = budgets.month_statuses(conn, datetime.now(UTC), tz)
        if not statuses:
            await message.reply_text(f"Nenhum orçamento definido.\n\n{BUDGET_USAGE}")
            return
        lines = ["💰 Orçamentos deste mês:"] + [format_budget_line(s) for s in statuses]
        await message.reply_text("\n".join(lines))


async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/exportar [MM[/AAAA]]: envia um CSV com todos os registros (ou de um mês)."""
    message = update.effective_message
    if message is None:
        return
    tz = _settings(context).timezone
    conn = _db(context)
    now = datetime.now(tz)

    if context.args:
        try:
            year, month_number = parse_month(" ".join(context.args), today=now.date())
        except PeriodError as exc:
            await message.reply_text(f"Não entendi o mês: {exc}.")
            return
        items = transactions.list_between(conn, *transactions.month_bounds(year, month_number, tz))
        scope, filename = month_label(year, month_number), f"finbot-{year}-{month_number:02d}.csv"
    else:
        items = transactions.list_all(conn)
        scope, filename = "todo o período", f"finbot-{now:%Y-%m-%d}.csv"

    if not items:
        await message.reply_text(f"Nenhum registro em {scope} para exportar.")
        return
    # O arquivo é montado na memória e enviado direto: nada fica salvo em disco.
    await message.reply_document(
        document=export.to_csv(items, tz),
        filename=filename,
        caption=f"📎 {len(items)} registro(s), {scope}.",
    )


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/desfazer: apaga o registro mais recente.

    Se o registro for o pagamento de uma dívida, desfaz os dois juntos: o gasto
    some e o saldo da dívida volta ao que era antes do pagamento.
    """
    message = update.effective_message
    if message is None:
        return
    conn = _db(context)
    last = transactions.last(conn)
    if last is None:
        await message.reply_text("Não há nada para desfazer.")
        return
    try:
        undone = debts.undo_payment(conn, last.id, _settings(context).timezone)
    except debts.UndoBlockedError as exc:
        await message.reply_text(
            f"O último registro é um pagamento da dívida {exc.debt_name}, mas o saldo dela "
            "mudou depois disso (correção com /saldo ou fatura nova). Desfazer agora "
            "apagaria essa mudança.\n"
            f"Corrija direto com /saldo {exc.debt_name} valor."
        )
        return
    if undone is not None:
        debt, event = undone
        await message.reply_text(
            f"🗑️ Apagado: pagamento de {format_brl(event.amount_cents)} em {debt.name}.\n"
            f"Saldo voltou para {format_brl(debt.balance_cents)}."
        )
        return
    t = transactions.delete_last(conn)
    await message.reply_text(f"🗑️ Apagado: {describe(t)}")
