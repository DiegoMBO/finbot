"""Testes da interpretação das mensagens."""

import pytest

from finbot.parsing.message import ParseError, parse_message


def test_gasto_com_verbo():
    e = parse_message("gastei 35 no mercado")
    assert (e.kind, e.amount_cents, e.category, e.description) == (
        "expense",
        3500,
        "alimentação",
        "mercado",
    )


def test_gasto_sem_verbo_com_centavos():
    e = parse_message("12,50 uber")
    assert (e.kind, e.amount_cents, e.category) == ("expense", 1250, "transporte")


def test_entrada_com_acento_e_maiuscula():
    e = parse_message("Recebi 3000 Salário")
    assert (e.kind, e.amount_cents, e.category, e.description) == (
        "income",
        300000,
        "salário",
        "Salário",
    )


def test_entrada_sem_palavra_conhecida_vai_para_receita():
    assert parse_message("recebi 50 da tia").category == "receita"


def test_categoria_desconhecida_vira_outros():
    e = parse_message("paguei 20 com o chaveiro")
    assert (e.category, e.description) == ("outros", "chaveiro")


def test_hashtag_forca_categoria():
    e = parse_message("80 presente #Família")
    assert (e.category, e.description) == ("família", "presente")


def test_so_o_primeiro_numero_e_o_valor():
    e = parse_message("15 no 99")
    assert (e.amount_cents, e.category, e.description) == (1500, "transporte", "99")


def test_valor_com_cifrao_separado_e_pontuacao():
    e = parse_message("R$ 1.234,56 aluguel!")
    assert (e.amount_cents, e.category, e.description) == (123456, "moradia", "aluguel")


@pytest.mark.parametrize("text", ["oi", "mercado", "gastei no mercado", "   "])
def test_sem_valor(text):
    with pytest.raises(ParseError, match="valor"):
        parse_message(text)


def test_valor_malformado():
    with pytest.raises(ParseError):
        parse_message("1,2,3 mercado")
