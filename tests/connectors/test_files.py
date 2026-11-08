from pathlib import Path

import pytest

from adp.connectors.files import list_sheets, read_file
from adp.errors import IngestionError

DATA = Path(__file__).parent.parent / "data"


def test_csv_latin1_semicolon() -> None:
    t = read_file(DATA / "clients_latin1.csv")
    assert t.columns == ["id", "nom", "ville", "montant"]
    assert t.rows[0] == ["1", "Hélène", "Orléans", "12,5"]
    assert t.rows[1] == ["2", "José", "Nîmes", None]
    assert t.meta["delimiter"] == ";"
    assert t.meta["encoding"] != "utf-8"
    assert t.meta["row_count"] == 4


def test_csv_utf8_comma() -> None:
    t = read_file(DATA / "people_utf8.csv")
    assert t.columns == ["id", "name", "city"]
    assert t.rows == [["1", "Alice", "Paris"], ["2", "Bob", "Lyon, FR"]]
    assert t.meta["delimiter"] == ","


def test_xlsx_multi_sheet() -> None:
    assert list_sheets(DATA / "multi.xlsx") == ["vide", "ventes", "autre"]
    t = read_file(DATA / "multi.xlsx")  # first non-empty sheet
    assert t.meta["sheet"] == "ventes"
    assert t.columns == ["id", "produit", "qte"]
    assert t.rows == [["1", "stylo", "10"], ["2", "cahier", None]]
    other = read_file(DATA / "multi.xlsx", sheet="autre")
    assert other.columns == ["k"] and other.rows == [["v"]]


def test_json_lines_and_array() -> None:
    t = read_file(DATA / "events.jsonl", fmt="json")
    assert t.columns == ["id", "kind", "extra"]
    assert t.rows == [["1", "a", None], ["2", "b", "True"]]
    a = read_file(DATA / "items.json")
    assert a.columns == ["id", "name", "price"]
    assert a.rows == [["1", "x", None], ["2", None, "2.5"]]


def test_errors(tmp_path: Path) -> None:
    with pytest.raises(IngestionError, match="missing.csv"):
        read_file(tmp_path / "missing.csv")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(IngestionError, match="bad.json"):
        read_file(bad)
    with pytest.raises(IngestionError, match="nope"):
        read_file(DATA / "multi.xlsx", sheet="nope")
    with pytest.raises(IngestionError, match="unsupported"):
        read_file(DATA / "multi.xlsx", fmt="parquet")
