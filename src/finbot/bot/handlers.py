"""Handlers: funções chamadas pelo Telegram quando chega um comando ou mensagem.

Regra do projeto: handlers são "finos". Eles só traduzem a mensagem do Telegram
para chamadas dos serviços e formatam a resposta. A lógica de verdade fica em
`services/` e `parsing/`, que podem ser testadas sem o Telegram.
"""

from __future__ import annotations

import difflib
import sqlite3
from datetime import UTC, datetime

from telegram import Message, Update
from telegram.ext import ContextTypes

from finbot.bot import card_ops, payments
from finbot.bot import salary as salary_bot
from finbot.services import salary as salary_service
from finbot.bot.texts import format_alert, format_budget_line
from finbot.config import Settings
from finbot.money import format_brl
from finbot.parsing.budget import USAGE as BUDGET_USAGE
from finbot.parsing.budget import BudgetArgsError, parse_budget_args
from finbot.parsing.message import ParseError, parse_message
from finbot.parsing.period import PeriodError, month_label, parse_month
from finbot.services import budgets, debts, export, transactions
from finbot.services import credit_cards as cc
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
    "Cartões de crédito:\n"
    "/novocartao: cadastra um cartão (fechamento, vencimento, limite...)\n"
    "/cartoes: todos os cartões e o melhor para usar hoje\n"
    "/cartao nome: limite, fatura atual, próximas e parcelamentos\n"
    "/fatura nome [valor]: mostra a fatura, ou informa o valor real do banco\n"
    "/parcelado: compra parcelada guiada (com juros embutidos)\n"
    "/estorno: estorno total ou parcial de uma compra\n"
    "/editarcartao nome: muda limite, datas, rotativo...\n"
    "Compras: cite o cartão. Ex.: gastei 80 no mercado no nubank · "
    "comprei tv 3000 em 10x no nubank · paguei 1500 da fatura do nubank\n\n"
    "Dívidas:\n"
    "/novadivida: cadastra uma dívida (perguntas uma a uma)\n"
    "/dividas: resumo de todas as dívidas\n"
    "/divida nome: detalhes e histórico de uma dívida\n"
    "/editardivida nome: muda taxa, parcela, vencimento e outros dados\n"
    "/removerdivida nome: apaga uma dívida (pede confirmação)\n"
    "/pagar nome valor: registra um pagamento (ou escreva: paguei 450 do empréstimo nubank)\n"
    "/saldo nome valor: corrige o saldo pelo valor do app do banco\n"
    "/simular nome extra: quanto você economiza pagando um extra por mês\n"
    "/estrategia [extra]: compara avalanche e bola de neve\n"
    "/semcartao [extra]: e se eu parar de usar os cartões?\n"
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


# Todos os comandos do bot (um teste confere que bate com os registrados em app.py).
KNOWN_COMMANDS = (
    "start", "ajuda", "help", "hoje", "mes", "desfazer", "orcamento", "exportar",
    "novadivida", "editardivida", "dividas", "divida", "removerdivida", "pagar", "saldo",
    "simular", "estrategia", "semcartao", "saude", "grafico", "lembretes", "cancelar",
    "novocartao", "editarcartao", "cartoes", "cartao", "fatura", "parcelado", "estorno",
    "salario", "editarsalario", "horasextras", "decimo", "ferias",
)  # fmt: skip


def suggest_command(typed: str) -> str | None:
    """O comando conhecido mais parecido com o digitado ("grafica" -> "grafico")."""
    matches = difflib.get_close_matches(typed.lower(), KNOWN_COMMANDS, n=1, cutoff=0.6)
    return matches[0] if matches else None


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando que não existe: em vez de silêncio, sugere o mais parecido."""
    message = update.effective_message
    if message is None or not message.text:
        return
    typed = message.text.split()[0].lstrip("/").split("@")[0]
    suggestion = suggest_command(typed)
    text = f"Não conheço o comando /{typed}."
    if suggestion:
        text += f" Você quis dizer /{suggestion}?"
    await message.reply_text(text + "\nTodos os comandos: /ajuda")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde /start e /ajuda com as instruções de uso."""
    # effective_message é a mensagem da atualização, seja ela qual for.
    if update.effective_message:
        await update.effective_message.reply_text(HELP_TEXT)


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Texto livre, nesta ordem:
    1. cita um cartão -> compra ou pagamento da fatura ("gastei 80 no mercado no nubank");
    2. paga uma dívida -> pagamento ("paguei 450 do empréstimo bradesco");
    3. senão, gasto/entrada comum ("gastei 35 no mercado")."""
    message = update.effective_message
    if message is None or not message.text:
        return
    if await salary_bot.try_overtime(message, context, message.text):
        return
    if await card_ops.try_card(message, context, message.text):
        return
    if await payments.try_payment(message, context, message.text):
        return
    await record_entry(message, context, message.text)


async def card_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botões "compra no cartão / pagamento da fatura / gasto comum"."""
    text = await card_ops.on_choice(update, context)
    if text is not None:
        await record_entry(update.callback_query.message, context, text)


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


def _card_action_is_newer(conn, action: cc.LastAction, last: Transaction | None) -> bool:
    """A ação no cartão aconteceu depois do último gasto comum?

    Os horários têm precisão de segundos; no empate, a compra decide pela ordem
    de gravação dos gastos (id). Sem gasto para comparar (pagamento), vale o cartão.
    """
    if last is None or action.occurred_at > last.created_at:
        return True
    if action.occurred_at < last.created_at:
        return False
    ref = cc.action_last_transaction_id(conn, action)
    return ref is None or ref > last.id


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/desfazer: apaga o registro mais recente.

    - Compra ou pagamento no cartão: desfaz a ação inteira (faturas, limite e gastos).
    - Pagamento de dívida: desfaz junto o gasto e o saldo da dívida.
    - Gasto/entrada comum: apaga.
    Vale o que aconteceu por último entre o cartão e os registros comuns.
    """
    message = update.effective_message
    if message is None:
        return
    conn = _db(context)
    card_action = cc.last_user_action(conn)
    # Gastos de compras no cartão são desfeitos pela compra, não um a um.
    last = transactions.last_excluding(conn, cc.card_transaction_ids(conn))
    # Hora extra registrada depois do último gasto/ação de cartão: apaga ela.
    overtime = salary_service.last_overtime(conn)
    if (
        overtime
        and (last is None or overtime[1] >= last.created_at)
        and (card_action is None or overtime[1] >= card_action.occurred_at)
    ):
        entry = overtime[0]
        salary_service.delete_overtime(conn, entry.id)
        await message.reply_text(
            f"🗑️ Apagado: {entry.minutes // 60}h{entry.minutes % 60:02d} extras de "
            f"{entry.worked_on:%d/%m/%Y}."
        )
        return
    if card_action and _card_action_is_newer(conn, card_action, last):
        await card_ops.undo_card_action(message, conn, card_action)
        return
    if last is None:
        await message.reply_text("Não há nada para desfazer.")
        return
    posted = salary_service.posted_by_transaction(conn, last.id)
    if posted is not None:  # salário/13º/férias lançado pelo bot (+ consignado junto)
        salary_service.undo_post(conn, posted, _settings(context).timezone)
        await message.reply_text(
            f"🗑️ Desfeito: {salary_service.KIND_LABEL[posted.kind]} ({posted.reference}), "
            f"{format_brl(posted.net_cents)}"
            + (" e o consignado junto." if posted.links else ".")
            + "\nO bot não lança de novo sozinho."
        )
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
    transactions.delete(conn, last.id)
    await message.reply_text(f"🗑️ Apagado: {describe(last)}")
