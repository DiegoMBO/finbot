"""/grafico nome: imagem com a evolução do saldo e a projeção futura.

Decisões de desenho:
- uma dívida = uma cor (azul, a 1ª da paleta padrão); o histórico é linha cheia
  com marcadores e a projeção é tracejada na MESMA cor (é a mesma dívida);
- rótulos escritos direto no gráfico ("histórico", "projeção"), sem caixa de
  legenda; o título diz qual é a dívida;
- grade e eixos discretos (cinza-claro), eixo y começando em zero, valores em R$;
- fundo claro: a imagem vai para o Telegram e não acompanha o tema do app.
Usamos `Figure` direto (sem pyplot): não depende de janela nem de estado global.
"""

from __future__ import annotations

import io
from datetime import date, datetime

from matplotlib.dates import AutoDateLocator, date2num, num2date
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter
from telegram import Update
from telegram.ext import ContextTypes

from finbot.bot.debts import short_month
from finbot.config import Settings
from finbot.money import format_brl
from finbot.parsing.period import MONTH_NAMES
from finbot.services import debts, evolution, finance
from finbot.services.debts import Debt
from finbot.services.evolution import Evolution

SERIES = "#257de0"  # paleta padrão, posição 1
CRITICAL = "#dc3c3d"  # status "crítico": dívida crescendo
SURFACE = "#ffffff"
INK = "#191919"
INK_2 = "#666666"
GRID = "#e7e7e7"
AXIS = "#c2c2c2"


def _reais(cents: float, _pos=None) -> str:
    # Eixo: sem centavos, com separador de milhar brasileiro.
    return f"R$ {round(cents / 100):,}".replace(",", ".")


def _month_tick(value: float, _pos=None) -> str:
    d = num2date(value)
    return f"{MONTH_NAMES[d.month - 1][:3]}\n{d.year}"


def subtitle(d: Debt, ev: Evolution) -> str:
    text = f"Saldo atual {format_brl(d.balance_cents)}"
    if ev.status == "quitavel" and ev.payoff:
        text += f" · quitação prevista em {short_month(ev.payoff)}"
    elif ev.status == "longa" or ev.truncated:
        text += " · projeção cortada em 10 anos"
    elif ev.status == "crescendo":
        text += " · a parcela não cobre os juros: a dívida CRESCE"
    elif ev.status == "estagnada":
        text += " · a parcela só cobre os juros"
    elif ev.status == "sem_pagamento":
        interest = finance.monthly_interest(d.balance_cents, d.rate_ppm)
        if interest:
            text += f" · sem pagamento: cresce {format_brl(interest)}/mês de juros"
        else:
            text += " · sem pagamento mensal: sem projeção"
    elif ev.status == "em_dia":
        text += " · em dia"
    return text


def render(d: Debt, ev: Evolution) -> bytes:
    fig = Figure(figsize=(8, 4.5), dpi=150, facecolor=SURFACE)
    ax = fig.add_subplot()
    ax.set_facecolor(SURFACE)
    projection_color = CRITICAL if ev.status == "crescendo" else SERIES

    # Fundo branco atrás dos rótulos: se encostarem numa linha, continuam legíveis.
    label_box = {"facecolor": SURFACE, "edgecolor": "none", "pad": 1.5}
    if ev.history:
        xs = [date2num(p[0]) for p in ev.history]
        ys = [p[1] for p in ev.history]
        ax.plot(xs, ys, color=SERIES, linewidth=2, marker="o", markersize=5, zorder=3)
        # Rótulo ABAIXO do primeiro ponto: a linha desce para a direita, então
        # o canto inferior esquerdo do primeiro ponto fica livre.
        ax.annotate(
            "histórico",
            (xs[0], ys[0]),
            textcoords="offset points",
            xytext=(0, -16),
            ha="left",
            va="top",
            color=INK_2,
            fontsize=9,
            bbox=label_box,
            zorder=4,
        )
    if ev.projection:
        xs = [date2num(p[0]) for p in ev.projection]
        ys = [p[1] for p in ev.projection]
        ax.plot(xs, ys, color=projection_color, linewidth=2, linestyle=(0, (4, 3)), zorder=2)
        middle = len(xs) // 2
        # Linha descendo: o rótulo vai acima e à direita (espaço livre); subindo,
        # vai abaixo e à direita, senão a própria linha passa por cima dele.
        rising = ys[-1] > ys[0]
        ax.annotate(
            "projeção",
            (xs[middle], ys[middle]),
            textcoords="offset points",
            xytext=(10, -8) if rising else (10, 6),
            ha="left",
            va="top" if rising else "bottom",
            color=INK_2,
            fontsize=9,
            bbox=label_box,
            zorder=4,
        )
        if ev.payoff:
            # Ponto final no zero: rótulo acima e à esquerda, afastado da linha.
            ax.plot([xs[-1]], [ys[-1]], marker="o", markersize=5, color=SERIES, zorder=3)
            ax.annotate(
                f"quitada em {short_month(ev.payoff)}",
                (xs[-1], ys[-1]),
                textcoords="offset points",
                xytext=(-8, 4),
                ha="right",
                va="bottom",
                color=INK,
                fontsize=9,
                bbox=label_box,
                zorder=4,
            )

    # Título e subtítulo alinhados à esquerda do gráfico (mesma margem do eixo).
    fig.suptitle(d.name, x=0.13, ha="left", y=0.97, fontsize=13, color=INK, fontweight="bold")
    ax.set_title(subtitle(d, ev), loc="left", fontsize=9, color=INK_2, pad=10)
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_formatter(FuncFormatter(_reais))
    ax.xaxis.set_major_locator(AutoDateLocator(maxticks=8))
    ax.xaxis.set_major_formatter(FuncFormatter(_month_tick))
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=INK_2, labelsize=8, length=0)
    fig.subplots_adjust(left=0.13, right=0.97, top=0.83, bottom=0.14)

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", facecolor=SURFACE)
    return buffer.getvalue()


async def chart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/grafico nome."""
    message = update.effective_message
    conn = context.bot_data["db"]
    settings: Settings = context.bot_data["settings"]
    name = " ".join(context.args or [])
    if not name:
        await message.reply_text("Use /grafico nome. Ex.: /grafico nubank")
        return
    found = debts.find(conn, name)
    if len(found) != 1:
        names = ", ".join(d.name for d in (found or debts.list_active(conn))) or "nenhuma"
        problem = "Não encontrei" if not found else "Mais de uma dívida combina com"
        await message.reply_text(f"{problem} {name!r}. Dívidas: {names}.")
        return
    d = found[0]
    today: date = datetime.now(settings.timezone).date()
    ev = evolution.build(d, debts.events(conn, d.id), today, settings.timezone)
    await message.reply_photo(photo=render(d, ev), caption=f"📉 {d.name}: {subtitle(d, ev)}")
