"""Testes do CSV do /exportar."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from finbot import db
from finbot.parsing.message import ParsedEntry
from finbot.services import export
from finbot.services import transactions as tx

SP = ZoneInfo("America/Sao_Paulo")


def test_csv_formato_brasileiro():
    items = [
        # 02:30 UTC do dia 27 = 23:30 do dia 26 em São Paulo
        tx.Transaction(
            1, "expense", 3500, "alimentação", "mercado", datetime(2026, 11, 27, 2, 30, tzinfo=UTC)
        ),
        tx.Transaction(
            2, "income", 123456, "salário", "", datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
        ),
    ]
    data = export.to_csv(items, SP)

    assert data.startswith(b"\xef\xbb\xbf")  # BOM: o Excel reconhece UTF-8
    lines = data.decode("utf-8-sig").splitlines()
    assert lines == [
        "data;hora;tipo;valor;categoria;descricao",
        "26/11/2026;23:30;gasto;35,00;alimentação;mercado",
        "27/11/2026;09:00;entrada;1234,56;salário;",
    ]


def test_csv_neutraliza_formulas_e_escapa_ponto_e_virgula():
    now = datetime(2026, 11, 26, tzinfo=UTC)
    items = [tx.Transaction(1, "expense", 100, "outros", "=HYPERLINK(1;2)", now)]
    last = export.to_csv(items, SP).decode("utf-8-sig").splitlines()[-1]
    # O ' impede a fórmula; as aspas mantêm o ";" dentro da mesma coluna.
    assert last.endswith(';outros;"\'=HYPERLINK(1;2)"')


def test_list_all_em_ordem():
    conn = db.connect(db.MEMORY)
    for cents in (100, 200, 300):
        tx.add(
            conn, ParsedEntry("expense", cents, "outros", ""), datetime(2026, 11, 26, tzinfo=UTC)
        )
    assert [t.amount_cents for t in tx.list_all(conn)] == [100, 200, 300]
    conn.close()
