"""Testes do roteiro de perguntas do cadastro e dos textos das dívidas."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from finbot.bot import debt_form as form
from finbot.bot.debts import (
    describe_projection,
    format_debt_details,
    format_debt_list,
    format_draft,
)
from finbot.services import debts

TODAY = date(2026, 11, 26)
SP = ZoneInfo("America/Sao_Paulo")


def fields_in_order(draft):
    result, current = [], None
    while (s := form.next_step(draft, current)) is not None:
        result.append(s.field)
        current = s.field
    return result


def test_ordem_das_perguntas():
    assert fields_in_order({"kind": "emprestimo"}) == [
        "name",
        "kind",
        "creditor",
        "balance_cents",
        "rate_ppm",
        "remaining_installments",
        "payment_cents",
        "due_day",
        "start_date",
    ]


def test_cartao_e_cheque_especial_nao_perguntam_parcelas():
    for kind in ("cartao", "cheque_especial"):
        assert "remaining_installments" not in fields_in_order({"kind": kind})


def test_obrigatorios_e_opcionais():
    required = {s.field for s in form.STEPS if not s.optional}
    assert required == {"name", "kind", "balance_cents"}


def test_pular_parcela_calcula_pela_price():
    # 10.000 a 2% em 12 parcelas = R$ 945,60 (conta à mão em test_finance)
    draft = {"balance_cents": 1_000_000, "rate_ppm": 20_000, "remaining_installments": 12}
    assert "R$ 945,60" in form.step("payment_cents").question(draft)
    form.apply(draft, form.step("payment_cents"), None)
    assert draft["payment_cents"] == 94_560
    assert draft["payment_computed"] is True


def test_pular_parcela_sem_numero_de_parcelas_fica_vazio():
    draft = {"balance_cents": 1_000_000, "rate_ppm": 20_000}
    form.apply(draft, form.step("payment_cents"), None)
    assert draft["payment_cents"] is None


def test_rascunho_vira_new_debt_com_padroes():
    new = form.to_new_debt(
        {"name": "Tia", "kind": "pessoa", "balance_cents": 5_000, "rate_ppm": None}
    )
    assert (new.rate_ppm, new.payment_cents, new.creditor) == (0, None, None)


# --- textos -----------------------------------------------------------------


def test_projecao_quitavel():
    # 5.000 a 3% pagando 300 = 24 pagamentos; vence dia 10 -> 10/12/2026 ... 10/11/2028
    text = describe_projection(500_000, 30_000, 30_000, 10, TODAY)
    assert text.startswith("Quitação em nov/2028 (24 pagamento(s))")


def test_projecao_crescendo_mostra_os_numeros():
    # 1.000 a 10% = juros de R$ 100; pagando R$ 50 cresce R$ 50 por mês
    text = describe_projection(100_000, 100_000, 5_000, 10, TODAY)
    assert "R$ 50,00 não cobre os juros do mês (R$ 100,00)" in text
    assert "CRESCE R$ 50,00 por mês" in text


def test_projecao_estagnada_e_sem_pagamento():
    assert "não diminui" in describe_projection(100_000, 100_000, 10_000, None, TODAY)
    assert "Sem pagamento" in describe_projection(100_000, 100_000, None, None, TODAY)


def _debt(**over):
    base = dict(
        id=1,
        name="Empréstimo Nubank",
        kind="emprestimo",
        creditor="Nubank",
        balance_cents=500_000,
        rate_ppm=30_000,
        payment_cents=30_000,
        due_day=10,
        remaining_installments=24,
        start_date=date(2025, 3, 15),
        status="ativa",
        created_at=datetime(2026, 11, 26, tzinfo=UTC),
    )
    return debts.Debt(**{**base, **over})


def test_lista_de_dividas():
    text = format_debt_list(
        [_debt(), _debt(id=2, name="Tia", kind="pessoa", rate_ppm=0, payment_cents=None)], TODAY
    )
    assert "1. Empréstimo Nubank · empréstimo" in text
    assert "Saldo R$ 5.000,00 · parcela R$ 300,00 · 3,00% a.m." in text
    assert "Total devido: R$ 10.000,00" in text
    assert "Parcelas por mês: R$ 300,00" in text
    assert "sem contar 1 dívida(s) sem projeção" in text


def test_lista_vazia():
    assert "/novadivida" in format_debt_list([], TODAY)


def test_detalhes():
    event = debts.DebtEvent(
        1, "cadastro", 500_000, 0, 0, 500_000, datetime(2026, 11, 26, 15, tzinfo=UTC)
    )
    text = format_debt_details(_debt(), [event], TODAY, SP)
    # 3% a.m. -> 1,03^12 − 1 = 42,58% a.a. ; juros do mês: 5.000 × 3% = R$ 150,00
    assert "Juros: 3,00% a.m. (42,5761% a.a.), R$ 150,00 no mês" in text
    assert "Vencimento: dia 10" in text
    assert "26/11/2026 cadastro: saldo R$ 5.000,00" in text
    assert "Confira o saldo" not in text  # 24 parcelas batem com o contrato


def test_detalhes_avisa_contrato_divergente():
    text = format_debt_details(_debt(remaining_installments=12), [], TODAY, SP)
    assert "seriam 24 parcelas, não 12" in text


def test_resumo_do_rascunho():
    draft = {
        "name": "Carro",
        "kind": "financiamento",
        "balance_cents": 1_000_000,
        "rate_ppm": 20_000,
        "remaining_installments": 12,
        "payment_cents": 94_560,
        "payment_computed": True,
        "due_day": 5,
    }
    text = format_draft(draft, TODAY)
    assert "Parcela: R$ 945,60 (calculada pela tabela Price)" in text
    assert "Credor: -" in text
    assert "Quitação em nov/2027 (12 pagamento(s))" in text  # 05/12/2026 + 11 meses
