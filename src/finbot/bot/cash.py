"""/extrato: extrato do mês (entra − dívidas − faturas − gastos = saldo).

/extrato                  mês atual
/extrato dez | próximo    outro mês (até 12 meses à frente)
/extrato inicial 350      quanto você tem na conta agora (pode ser negativo: -120)
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from telegram import Update
from telegram.ext import ContextTypes

from finbot.config import Settings
from finbot.money import AmountError, format_brl, parse_amount
from finbot.parsing.period import MONTH_NAMES, month_label
from finbot.parsing.text import normalize
from finbot.services import cash, finance
from finbot.services.cash import DEBT, DONE, EXPENSE, INCOME, INVOICE, LATE, Statement

USAGE = (
    "Use: /extrato (mês atual), /extrato dez ou /extrato próximo (outro mês), "
    "/extrato inicial 350 (quanto tem na conta agora; negativo: /extrato inicial -120)"
)

GROUPS = (
    (INCOME, "💰 Entra"),
    (DEBT, "💸 Dívidas"),
    (INVOICE, "💳 Faturas"),
    (EXPENSE, "🛒 Gastos registrados (fora do cartão)"),
)


class MonthArgError(ValueError):
    pass


def parse_target(arg: str, today: date) -> tuple[int, int]:
    """'dez', 'dezembro', '12', '12/2026', 'próximo', 'anterior' -> (ano, mês).
    Sem ano, o mês é o próximo com esse número (em novembro, 'jan' = janeiro seguinte)."""
    norm = normalize(arg).strip()
    if norm in ("proximo", "seguinte"):
        return finance.add_months(today.year, today.month, 1)
    if norm == "anterior":
        return finance.add_months(today.year, today.month, -1)
    if norm in ("atual", "este", "hoje"):
        return today.year, today.month
    year = None
    match = re.fullmatch(r"(\d{1,2})(?:/(\d{2}|\d{4}))?", norm)
    if match:
        month = int(match.group(1))
        if match.group(2):
            year = int(match.group(2))
            year += 2000 if year < 100 else 0
    else:
        names = [normalize(n)[:3] for n in MONTH_NAMES]
        if norm[:3] not in names:
            raise MonthArgError(f"não entendi o mês {arg!r}")
        month = names.index(norm[:3]) + 1
    if not 1 <= month <= 12:
        raise MonthArgError(f"mês {month} não existe")
    if year is None:
        year = today.year if month >= today.month else today.year + 1
    return year, month


def parse_signed_amount(text: str) -> int:
    raw = text.strip().replace("−", "-")
    sign = -1 if raw.startswith("-") else 1
    raw = raw.lstrip("-+ ").strip()
    if raw in ("0", "0,00", "0.00"):
        return 0
    return sign * parse_amount(raw)


def _signed(cents: int) -> str:
    return f"−{format_brl(-cents)}" if cents < 0 else format_brl(cents)


def _item_line(i: cash.Item) -> str:
    day = f"{i.day:%d/%m} " if i.day else ""
    note = f" ({i.note})" if i.note else ""
    late = " — venceu sem pagamento registrado" if i.status == LATE else ""
    return f"   {i.status} {day}{i.label}: {format_brl(i.amount_cents)}{note}{late}"


def _nav(year: int, month: int, today: date) -> str:
    """Comando que abre esse mês: '/extrato jan' (com ano se for além de 11 meses)."""
    name = normalize(MONTH_NAMES[month - 1])[:3]
    far = cash.months_between((today.year, today.month), (year, month)) >= 12
    return f"/extrato {month}/{year}" if far else f"/extrato {name}"


def _dm(d: date) -> str:
    return f"{d:%d/%m}"


def _format_outlook(o: cash.Outlook) -> list[str]:
    lines = []
    until = (
        f"até o próximo dinheiro ({_dm(o.next_income_day)})"
        if o.next_income_day
        else "até o fim do mês seguinte (sem entrada prevista)"
    )
    if o.committed:
        lines.append(f"   Contas {until}: −{format_brl(o.committed_cents)}")
        lines.append("   (" + ", ".join(f"{i.label} {_dm(i.day)}" for i in o.committed) + ")")
    # O dinheiro do fim do mês já tem dono: as contas que vencem antes de entrar mais.
    if o.free_cents < 0:
        lines.append(f"🚨 Faltam {format_brl(-o.free_cents)} para pagar as contas {until}")
    else:
        icon = "⚠️" if o.free_cents < 30_000 else "✅"
        lines.append(f"{icon} Sobram {format_brl(o.free_cents)} depois de pagar as contas {until}")

    lines.append("")
    if o.negative:
        lines.append(f"📉 Ponto mais baixo: {_signed(o.low_cents)} em {_dm(o.low_day)}")
        parts = [
            f"de {_dm(a)} a {_dm(b)}"
            if b and b != a
            else (f"em {_dm(a)}" if b else f"a partir de {_dm(a)}")
            for a, b in o.negative
        ]
        joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " e " + parts[-1]
        lines.append("   Fica negativo " + joined)
        lines.append(
            "   Sem dinheiro nesses dias, uma conta atrasa ou uma fatura vai para o rotativo."
        )
    else:
        lines.append(
            f"📉 Ponto mais baixo: {_signed(o.low_cents)} em {_dm(o.low_day)} "
            f"(não fica negativo até {_dm(o.until)})"
        )
    return lines


def pct(ppm: int) -> str:
    """80000 -> '8%'; 1518170 -> '151,8%' (1 casa, sem zeros à toa)."""
    text = f"{ppm / 10_000:.1f}".replace(".", ",")
    return (text[:-2] if text.endswith(",0") else text) + "%"


def format_priority(g: cash.Growing) -> list[str]:
    """Dívida que cresce sozinha (cheque especial): em destaque, no topo do extrato."""
    return [
        f"🔥 PRIORIDADE: {g.name} cresce {format_brl(g.interest_cents)} neste mês",
        f"   Deve {format_brl(g.balance_cents)} a {pct(g.rate_ppm)} ao mês "
        f"(≈ {pct(g.annual_ppm)} ao ano), sem parcela: os juros entram na dívida.",
        f"   Sem pagar, vira {format_brl(g.projected(6))} em 6 meses e "
        f"{format_brl(g.projected(12))} em 12.",
        f"   Cada R$ 100 que você abate economiza {format_brl(g.rate_ppm // 100)} por mês. "
        "É a primeira dívida a quitar com o que sobrar.",
    ]


def format_statement(s: Statement, today: date, o: cash.Outlook | None = None) -> str:
    title = month_label(s.year, s.month)
    lines = [f"📒 {title[0].upper()}{title[1:]}"]
    if s.start_cents is not None:
        lines.append(f"{s.start_label}: {_signed(s.start_cents)}")
    for g in s.growing:
        lines += [""] + format_priority(g)
    for group, name in GROUPS:
        items = sorted(
            (i for i in s.items if i.group == group), key=lambda i: (i.day is None, i.day or today)
        )
        lines += ["", f"{name}: {format_brl(s.total(group))}"]
        if group == EXPENSE and not items:
            lines.append("   nenhum ainda: comida, contas e compras que você registrar saem daqui")
            continue
        lines += [_item_line(i) for i in items] or ["   nada neste mês"]

    lines.append("")
    if s.end_cents is not None and o is not None:
        lines.append(f"= Fim do mês: {_signed(s.end_cents)}")
        lines.append(f"   (resultado só deste mês: {_signed(s.result_cents)})")
        lines += _format_outlook(o)
        if s.growing and o.free_cents > 0:
            g = max(s.growing, key=lambda x: x.rate_ppm)  # a mais cara primeiro
            pay = g.suggested_payment(o.free_cents)
            lines.append(
                f"💡 Com o que sobra, mande até {format_brl(pay)} para o {g.name} "
                f"(economiza {format_brl(finance.monthly_interest(pay, g.rate_ppm))} de juros "
                "por mês)."
            )
    elif s.end_cents is not None:
        icon = "🚨" if s.end_cents < 0 else "✅"
        lines.append(f"= Fim do mês: {_signed(s.end_cents)} {icon}")
        lines.append(f"   (resultado só deste mês: {_signed(s.result_cents)})")
        if s.end_cents < 0:
            lines.append(
                f"🚨 Faltam {format_brl(-s.end_cents)}: sem esse dinheiro, a conta fica no "
                "negativo (cheque especial) ou uma fatura vai para o rotativo."
            )
    else:
        icon = "🚨" if s.result_cents < 0 else "✅"
        lines.append(f"= Resultado do mês (entra − sai): {_signed(s.result_cents)} {icon}")
        if (s.year, s.month) >= (today.year, today.month):
            lines.append("Para ver quanto sobra de verdade, informe o que tem na conta:")
            lines.append("/extrato inicial 350")

    if s.undated_cards:
        parts = " · ".join(f"{name} {format_brl(v)}" for name, v in s.undated_cards)
        lines += [
            "",
            f"⚠️ Cartões: {format_brl(s.undated_cents)} de parcelas ainda sem mês definido",
            f"   ({parts})",
            "   Elas vão cair nas próximas faturas, que podem ser maiores do que aparece aqui.",
            "   Quando cada fatura fechar, informe o valor real: /fatura nome valor",
        ]

    has_late = any(i.status == LATE for i in s.items)
    legend = f"{DONE} feito · ⏳ previsto" + (f" · {LATE} atrasado" if has_late else "")
    # Navegação relativa ao mês MOSTRADO (não a hoje): anterior · seguinte
    prev_y, prev_m = finance.add_months(s.year, s.month, -1)
    next_y, next_m = finance.add_months(s.year, s.month, 1)
    nav = [_nav(prev_y, prev_m, today)] if (prev_y, prev_m) >= (today.year, today.month) else []
    nav.append(_nav(next_y, next_m, today))
    lines += ["", legend, "Outro mês: " + " · ".join(nav)]
    return "\n".join(lines)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    settings: Settings = context.bot_data["settings"]
    conn = context.bot_data["db"]
    tz = settings.timezone
    now = datetime.now(UTC)
    today = now.astimezone(tz).date()
    args = list(context.args or [])

    if args and normalize(args[0]) == "inicial":
        if len(args) == 1:
            anchor = cash.get_anchor(conn)
            if anchor is None:
                await message.reply_text(
                    "Informe quanto tem na conta agora. Ex.: /extrato inicial 350 "
                    "(negativo: /extrato inicial -120)"
                )
            else:
                when = anchor.informed_at.astimezone(tz)
                await message.reply_text(
                    f"Saldo inicial: {_signed(anchor.amount_cents)}, informado em "
                    f"{when:%d/%m/%Y %H:%M}. Para atualizar: /extrato inicial 350"
                )
            return
        try:
            amount = parse_signed_amount(" ".join(args[1:]))
        except AmountError:
            await message.reply_text("Não entendi o valor. Ex.: /extrato inicial 350")
            return
        cash.set_anchor(conn, amount, now)
        s = cash.statement(conn, today.year, today.month, today, tz, settings.extra_holidays)
        await message.reply_text(
            f"✅ Saldo inicial: {_signed(amount)} (agora). Tudo o que você registrar a "
            "partir de agora mexe nesse saldo.\n\n"
            + format_statement(
                s,
                today,
                cash.outlook(conn, today.year, today.month, today, tz, settings.extra_holidays),
            )
        )
        return

    try:
        year, month = parse_target(" ".join(args), today) if args else (today.year, today.month)
    except MonthArgError as exc:
        await message.reply_text(f"{exc}. {USAGE}")
        return
    if cash.months_between((today.year, today.month), (year, month)) > cash.MAX_MONTHS_AHEAD:
        await message.reply_text(f"Consigo prever até {cash.MAX_MONTHS_AHEAD} meses à frente.")
        return
    s = cash.statement(conn, year, month, today, tz, settings.extra_holidays)
    o = cash.outlook(conn, year, month, today, tz, settings.extra_holidays)
    await message.reply_text(format_statement(s, today, o))
