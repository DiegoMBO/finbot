"""/saude: indicadores de saúde financeira e sugestões com os números à mostra."""

from __future__ import annotations

from dataclasses import dataclass
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


def _ref(reference_month: str) -> str:
    """'2026-12' -> 'dez/2026'."""
    year, month = (int(x) for x in reference_month.split("-"))
    return _short((year, month))


@dataclass(frozen=True)
class CardsSummary:
    used_cents: int
    limit_cents: int
    overdue_cents: int


def cards_summary(conn, today: date) -> CardsSummary | None:
    cards = cc.list_cards(conn)
    if not cards:
        return None
    return CardsSummary(
        used_cents=sum(c.used_cents for c in cards),
        limit_cents=sum(c.limit_cents for c in cards),
        overdue_cents=sum(
            i.remaining_cents for c in cards for i in cc.overdue_invoices(conn, c, today)
        ),
    )


def _leftover_icon(value: int, income: int) -> str:
    """🚨 negativo · ⚠️ menos de 10% da renda (qualquer imprevisto estoura) · ✅."""
    if value < 0:
        return "🚨"
    return "⚠️" if value * 10 < income else "✅"


def format_indicators(
    i: Indicators, cfg: HealthSettings, cards: CardsSummary | None = None
) -> list[str]:
    """Do mais importante para o detalhe: veredito → conta do mês → meses com
    as faturas antigas → quanto deve → de onde vêm os números."""
    month_now = month_label(*i.months[-1]) if i.partial else None
    lines = ["🩺 Saúde financeira", ""]

    # 1. Veredito em uma frase
    if i.commitment_bp is None:
        lines.append("Ainda não sei sua renda: cadastre o salário (/salario) ou registre")
        lines.append("as entradas (ex.: recebi 3000 salário).")
    else:
        lines.append(f"{BAND_ICONS[i.band]} {i.band.capitalize()}")
        lines.append(
            f"As parcelas das dívidas levam {format_bp(i.commitment_bp)} da sua renda "
            f"(saudável: até {cfg.ok_max_pct}%; atenção: até {cfg.warn_max_pct}%)."
        )

    # 2. A conta de um mês típico
    if i.income_cents:
        lines += ["", "💰 Seu mês, em média"]
        if i.salary_cents is not None:
            others = i.income_cents - i.salary_cents
            if others:
                lines.append(f"Entra: {format_brl(i.income_cents)}")
                lines.append(
                    f"   salário líquido {format_brl(i.salary_cents)} "
                    f"+ outras entradas {format_brl(others)}"
                )
            else:
                lines.append(f"Entra (salário líquido): {format_brl(i.income_cents)}")
        else:
            lines.append(
                f"Entra: {format_brl(i.income_cents)} (média; cadastre o salário: /salario)"
            )
        lines.append(f"Parcelas das dívidas: −{format_brl(i.installments_cents)}")
        if i.installment_debts:
            lines.append(f"   {', '.join(i.installment_debts)}")
        fixed_note = " (nenhum registrado ainda)" if i.fixed_cents == 0 else ""
        lines.append(f"Gastos fixos: −{format_brl(i.fixed_cents)}{fixed_note}")
        label = "Sobra antes das faturas antigas" if i.card_pre_bot_months else "Sobra"
        icon = (
            "" if i.card_pre_bot_months else " " + _leftover_icon(i.leftover_cents, i.income_cents)
        )
        lines.append(f"{label}: {format_brl(i.leftover_cents)}{icon}")

    # 3. Faturas antigas: tiram da sobra, mês a mês, e vão diminuindo
    if i.card_pre_bot_months:
        lines += ["", "💳 Faturas de compras de antes do bot"]
        if i.income_cents:
            lines.append("Saem da sobra acima. Como elas diminuem, cada mês fica diferente:")
            for (m, bill), (_, left) in zip(
                i.card_pre_bot_months, i.leftover_by_month(), strict=True
            ):
                lines.append(
                    f"{_ref(m)}: fatura {format_brl(bill)} → sobra {format_brl(left)} "
                    f"{_leftover_icon(left, i.income_cents)}"
                )
        else:
            for m, bill in i.card_pre_bot_months:
                lines.append(f"{_ref(m)}: {format_brl(bill)}")

    # 4. Quanto deve no total
    lines += ["", "🧾 Quanto você deve"]
    lines.append(f"Dívidas: {format_brl(i.total_debt_cents)}")
    if cards is not None or i.card_used_cents:
        text = f"Cartões: {format_brl(i.card_used_cents)}"
        if cards is not None and cards.limit_cents:
            text += (
                f" ({format_bp(cards.used_cents * 10_000 // cards.limit_cents)} do limite "
                f"de {format_brl(cards.limit_cents)})"
            )
        lines.append(text)
        if cards is not None and cards.overdue_cents:
            lines.append(f"🚨 Fatura vencida sem pagar: {format_brl(cards.overdue_cents)}")
        lines.append(f"Total: {format_brl(i.total_with_cards_cents)}")

    # 5. De onde vêm os números (no fim, pequeno)
    lines.append("")
    if i.partial:
        base = f"ℹ️ Base: {month_now} até hoje (ainda não há mês completo com registros)."
    else:
        months = ", ".join(_short(m) for m in i.months)
        base = f"ℹ️ Base: média de {months}."
    if i.card_pre_bot_months and i.income_cents:
        base += (
            " As sobras são de um mês típico, sem 13º e férias; "
            "o caixa exato de cada mês está em /estrategia sobra."
        )
    lines.append(base)
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
    indicators = health.indicators(conn, today, settings.timezone, cfg)
    lines = format_indicators(indicators, cfg, cards_summary(conn, today))
    lines.append("")
    lines += format_suggestions(health.suggestions(conn, today, settings.timezone, cfg), cfg, today)
    await update.effective_message.reply_text("\n".join(lines))
