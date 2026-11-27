"""/saude: indicadores de saúde financeira e sugestões com os números à mostra."""

from __future__ import annotations

from datetime import date, datetime

from telegram import Update
from telegram.ext import ContextTypes

from finbot.bot.debts import short_month
from finbot.config import HealthSettings, Settings
from finbot.money import format_brl, format_percent
from finbot.parsing.period import MONTH_NAMES, month_label
from finbot.services import credit_cards as cc
from finbot.services import finance, health
from finbot.services.health import Indicators, Month, Suggestion, SuggestionReport

BAND_ICONS = {"saudável": "🟢", "atenção": "🟡", "alerta": "🔴"}


def format_bp(bp: int) -> str:
    """Pontos-base com 1 casa: 2491 -> "24,9%"."""
    tenths = (bp + 5) // 10
    return f"{tenths // 10},{tenths % 10}%"


def _short(m: Month) -> str:
    return f"{MONTH_NAMES[m[1] - 1][:3]}/{m[0]}"


def format_indicators(i: Indicators, cfg: HealthSettings) -> list[str]:
    if i.partial:
        base = (
            f"Base: {month_label(*i.months[0])} até hoje (ainda não há mês completo com registros)"
        )
    else:
        months = ", ".join(_short(m) for m in i.months)
        base = f"Base: média de {months} ({len(i.months)} mês(es) completo(s))"
    lines = ["🩺 Saúde financeira", base, ""]
    lines.append(f"Renda média: {format_brl(i.income_cents)}")
    lines.append(
        f"Gastos fixos médios ({', '.join(cfg.fixed_categories)}): {format_brl(i.fixed_cents)}"
    )
    debts_text = ", ".join(i.installment_debts) if i.installment_debts else "nenhuma"
    lines.append(f"Parcelas de dívidas: {format_brl(i.installments_cents)} ({debts_text})")
    if i.commitment_bp is None:
        lines.append(
            "Comprometimento da renda: sem entradas registradas no período. "
            "Registre com, por exemplo: recebi 3000 salário"
        )
    else:
        lines.append(
            f"Comprometimento da renda: {format_bp(i.commitment_bp)} → "
            f"{BAND_ICONS[i.band]} {i.band} "
            f"(saudável até {cfg.ok_max_pct}%, atenção até {cfg.warn_max_pct}%)"
        )
    if i.income_cents == 0:
        # Sem renda registrada, a "sobra" seria negativa só por falta de dado: sem alarme.
        lines.append(
            "Sobra depois dos fixos e das parcelas: registre suas entradas para eu calcular"
        )
    else:
        lines.append(f"Sobra depois dos fixos e das parcelas: {format_brl(i.leftover_cents)}")
        if i.leftover_cents < 0:
            lines.append("🚨 Os fixos e as parcelas passam da renda média.")
    lines.append(f"Total em dívidas: {format_brl(i.total_debt_cents)}")
    lines.append(
        "(Cartão de crédito não entra nas parcelas: a fatura é consumo e já está nos gastos.)"
    )
    return lines


def _format_suggestion(n: int, s: Suggestion, last: Month, today: date) -> list[str]:
    month_name = MONTH_NAMES[last[1] - 1]
    if s.growth_pct is None:
        head = (
            f"{n}. {s.category}: {format_brl(s.last_cents)} em {month_name}, "
            "sem gastos nessa categoria nos meses anteriores."
        )
    else:
        head = (
            f"{n}. {s.category}: {format_brl(s.last_cents)} em {month_name} contra média de "
            f"{format_brl(s.average_cents)} (+{format_brl(s.growth_cents)}, +{s.growth_pct}%)."
        )
    lines = [head]
    if s.target is None or s.simulation is None:
        lines.append("   Sem dívidas com saldo para direcionar esse valor.")
        return lines
    t = s.target
    rate = f"{format_percent(t.rate_ppm)} a.m." if t.rate_ppm else "sem juros"
    prefix = (
        f"   Mandando {format_brl(s.redirect_cents)}/mês para {t.name} ({rate}, a maior taxa): "
    )
    sim = s.simulation
    if sim.months_saved is not None:
        end = finance.payoff_date(today, sim.with_extra.months, t.due_day)
        lines.append(
            prefix
            + f"quita {sim.months_saved} mês(es) antes ({short_month(end)}) e economiza "
            + f"{format_brl(sim.interest_saved_cents)} de juros."
        )
    elif sim.with_extra.status == "quitavel":
        lines.append(prefix + "a dívida deixa de crescer e passa a ter data para acabar.")
    else:
        lines.append(prefix + "ainda não cobre os juros do mês; seria preciso mais.")
    return lines


def format_suggestions(r: SuggestionReport, cfg: HealthSettings, today: date) -> list[str]:
    title = f"💡 Sugestões ({month_label(*r.last_month)} contra a média dos meses anteriores)"
    if not r.base_months:
        return [
            "💡 Sugestões: ainda não há meses anteriores suficientes para comparar "
            f"(preciso de registros em {month_label(*r.last_month)} e em pelo menos um mês antes)."
        ]
    lines = [title]
    lines.append(
        f"Regra: categorias que cresceram pelo menos {cfg.min_growth_pct}% e "
        f"{format_brl(cfg.min_growth_cents)}; simulando {cfg.share_pct}% do aumento na dívida. "
        f"Média de: {', '.join(_short(m) for m in r.base_months)}."
    )
    if not r.items:
        lines.append("Nenhuma categoria cresceu acima desses limites. 👍")
        return lines
    for n, s in enumerate(r.items, 1):
        lines += _format_suggestion(n, s, r.last_month, today)
    return lines


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    today = datetime.now(settings.timezone).date()
    cfg = settings.health
    lines = format_indicators(health.indicators(conn, today, settings.timezone, cfg), cfg)
    card_line = format_cards_line(conn, today)
    if card_line:
        lines.append(card_line)
    lines.append("")
    lines += format_suggestions(health.suggestions(conn, today, settings.timezone, cfg), cfg, today)
    await update.effective_message.reply_text("\n".join(lines))


def format_cards_line(conn, today: date) -> str | None:
    """Cartões: uso do limite e faturas a pagar. As COMPRAS já estão nos gastos;
    aqui é só para você ver quanto do limite está comprometido."""
    cards = cc.list_cards(conn)
    if not cards:
        return None
    used = sum(c.used_cents for c in cards)
    limit = sum(c.limit_cents for c in cards)
    current = sum(inv.remaining_cents for c in cards if (inv := cc.current_invoice(conn, c, today)))
    overdue = sum(i.remaining_cents for c in cards for i in cc.overdue_invoices(conn, c, today))
    text = (
        f"Cartões: usado {format_brl(used)} de {format_brl(limit)} "
        f"({format_bp(used * 10_000 // limit)}) · faturas atuais a pagar {format_brl(current)}"
    )
    if overdue:
        text += f" · 🚨 vencido {format_brl(overdue)}"
    return text
