"""/simular (valor extra numa dívida) e /estrategia (avalanche × bola de neve).

Os cálculos estão em services/finance.py (testados com contas à mão); aqui só
montamos os dados de entrada e o texto da resposta.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from telegram import Update
from telegram.ext import ContextTypes

from finbot.bot.debts import short_month
from finbot.config import Settings
from finbot.money import AmountError, format_brl, format_percent, parse_amount
from finbot.parsing.payment import PaymentArgsError, parse_name_and_amount
from finbot.services import debts, finance
from finbot.services.debts import Debt
from finbot.services.finance import ExtraSimulation, Projection, StrategyResult

STRATEGY_TITLES = {
    "avalanche": "🏔️ Avalanche (maior juros primeiro)",
    "bola_de_neve": "⛄ Bola de neve (menor saldo primeiro)",
}


def _db(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    return context.bot_data["db"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


def month_after(today: date, k: int) -> date:
    year, month = finance.add_months(today.year, today.month, k)
    return date(year, month, 1)


# --- /simular ---------------------------------------------------------------


def _scenario(label: str, monthly_cents: int, p: Projection, today: date, due_day) -> str:
    head = f"{label}: {format_brl(monthly_cents)}/mês"
    if p.status == "quitavel":
        end = finance.payoff_date(today, p.months, due_day)
        return (
            f"{head} → quitação em {short_month(end)} ({p.months} pagamento(s)), "
            f"juros {format_brl(p.total_interest_cents)}"
        )
    if p.status == "crescendo":
        return f"{head} → a dívida CRESCE {format_brl(p.growth_per_month_cents)} por mês"
    if p.status == "estagnada":
        return f"{head} → só cobre os juros: o saldo não diminui"
    if p.status == "longa":
        return f"{head} → mais de 100 anos para quitar"
    return f"{head} → sem pagamento, não dá para projetar"


def format_simulation(d: Debt, extra_cents: int, s: ExtraSimulation, today: date) -> str:
    base = d.payment_cents or 0
    lines = [
        f"🧮 Simulação: {d.name} com +{format_brl(extra_cents)} por mês",
        f"Saldo {format_brl(d.balance_cents)} · juros "
        + (f"{format_percent(d.rate_ppm)} a.m." if d.rate_ppm else "sem juros"),
        "",
        _scenario("Hoje", base, s.base, today, d.due_day),
        _scenario("Com extra", base + extra_cents, s.with_extra, today, d.due_day),
        "",
    ]
    interest = finance.monthly_interest(d.balance_cents, d.rate_ppm)
    if s.months_saved is not None:
        lines.append(
            f"💰 Economia: {s.months_saved} mês(es) a menos e "
            f"{format_brl(s.interest_saved_cents)} a menos de juros."
        )
    elif s.with_extra.status == "quitavel":
        lines.append("💰 Com o extra a dívida passa a diminuir e tem data para acabar.")
    else:
        lines.append(
            f"Mesmo com o extra, o pagamento não passa dos juros do mês ({format_brl(interest)}). "
            f"Seria preciso pagar mais de {format_brl(interest)} por mês."
        )
    return "\n".join(lines)


async def simulate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/simular nome valor_extra."""
    message = update.effective_message
    try:
        name, extra = parse_name_and_amount(list(context.args or []))
    except PaymentArgsError as exc:
        await message.reply_text(f"Não entendi: {exc}.\nEx.: /simular nubank 200")
        return
    conn = _db(context)
    found = debts.find(conn, name)
    if len(found) != 1:
        names = ", ".join(d.name for d in (found or debts.list_active(conn))) or "nenhuma"
        problem = "Não encontrei" if not found else "Mais de uma dívida combina com"
        await message.reply_text(f"{problem} {name!r}. Dívidas: {names}.")
        return
    d = found[0]
    simulation = finance.simulate_extra(d.balance_cents, d.rate_ppm, d.payment_cents, extra)
    today = datetime.now(_settings(context).timezone).date()
    await message.reply_text(format_simulation(d, extra, simulation, today))


# --- /estrategia ------------------------------------------------------------


def strategy_inputs(items: list[Debt]) -> list[finance.DebtInput]:
    return [
        finance.DebtInput(d.name, d.balance_cents, d.rate_ppm, d.payment_cents or 0)
        for d in items
        if d.balance_cents > 0
    ]


def _format_result(r: StrategyResult, rates: dict[str, int], today: date) -> list[str]:
    lines = [STRATEGY_TITLES[r.strategy]]
    if not r.feasible:
        lines.append("Com esse orçamento as dívidas não acabam (os juros comem o pagamento).")
        return lines
    for i, name in enumerate(r.payoff_order, 1):
        k = r.payoff_month[name]
        rate = f"{format_percent(rates[name])} a.m." if rates[name] else "sem juros"
        lines.append(
            f"{i}. {name} ({rate}): quitada em {short_month(month_after(today, k))} (mês {k})"
        )
    lines.append(
        f"Tudo quitado em {short_month(month_after(today, r.total_months))} "
        f"({r.total_months} meses) · juros: {format_brl(r.total_interest_cents)}"
    )
    return lines


def format_strategies(inputs: list[finance.DebtInput], extra_cents: int, today: date) -> str:
    if not inputs:
        return "Nenhuma dívida com saldo para comparar. 🎉"
    avalanche, snowball = finance.compare_strategies(inputs, extra_cents)
    rates = {d.name: d.rate_ppm for d in inputs}
    installments = sum(d.payment_cents for d in inputs)
    budget = f"Orçamento mensal: {format_brl(avalanche.monthly_budget_cents)}"
    budget += f" (parcelas {format_brl(installments)}"
    budget += f" + extra {format_brl(extra_cents)})" if extra_cents else ")"
    lines = ["⚖️ Avalanche × bola de neve", budget, ""]
    lines += _format_result(avalanche, rates, today)
    lines.append("")
    lines += _format_result(snowball, rates, today)
    lines.append("")

    if not avalanche.feasible or not snowball.feasible:
        interest = sum(finance.monthly_interest(d.balance_cents, d.rate_ppm) for d in inputs)
        lines.append(
            f"Os juros do primeiro mês somam {format_brl(interest)}. Teste com um valor extra, "
            "ex.: /estrategia 300"
        )
    elif len(inputs) == 1:
        lines.append("Com uma dívida só, as duas estratégias dão o mesmo resultado.")
    else:
        diff = snowball.total_interest_cents - avalanche.total_interest_cents
        months = snowball.total_months - avalanche.total_months
        if diff > 0:
            text = f"👉 A avalanche paga {format_brl(diff)} a menos de juros"
            text += f" e termina {months} mês(es) antes." if months > 0 else "."
        elif diff < 0:
            text = f"👉 A bola de neve paga {format_brl(-diff)} a menos de juros."
        else:
            text = "👉 As duas pagam os mesmos juros."
        first_a = min(avalanche.payoff_month.values())
        first_s = min(snowball.payoff_month.values())
        if first_s < first_a:
            text += (
                f"\nA bola de neve quita a primeira dívida {first_a - first_s} mês(es) antes, "
                "o que ajuda a manter a motivação."
            )
        lines.append(text)
    if not extra_cents:
        lines.append("Para simular com dinheiro a mais por mês: /estrategia 200")
    return "\n".join(lines)


async def strategy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/estrategia [valor_extra]."""
    message = update.effective_message
    extra = 0
    if context.args:
        try:
            extra = parse_amount(" ".join(context.args))
        except AmountError:
            await message.reply_text("Não entendi o valor extra. Ex.: /estrategia 200")
            return
    inputs = strategy_inputs(debts.list_active(_db(context)))
    today = datetime.now(_settings(context).timezone).date()
    await message.reply_text(format_strategies(inputs, extra, today))
