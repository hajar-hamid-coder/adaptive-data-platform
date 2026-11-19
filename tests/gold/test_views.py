"""Gold views and indicators (P5-02) on a tiny hand-made dataset (tests/data/gold)."""

import json
from pathlib import Path

import duckdb
import pytest

from adp.bronze.store import LocalStore
from adp.cli import main
from adp.errors import GoldError
from adp.gold.contract import load_products
from adp.gold.views import build_all, build_product, run_indicator
from adp.silver.writer import DUCKDB_TYPES

DATA = Path(__file__).parents[1] / "data" / "gold"
PRODUCTS = {p.name: p for p in load_products()}


def _promote(store: LocalStore, source: str, batch: str) -> None:
    """Mimic the promotion gate: parquet from the CSV + manifest under gold_ready/."""
    data = _parquet(DATA / f"{source}_{batch}.csv", store.root.parent)
    store.put_if_absent(f"gold_ready/{source}/{batch}/data.parquet", data)
    store.put_if_absent(f"gold_ready/{source}/{batch}/manifest.json", b"{}")


def _parquet(csv: Path, tmp: Path) -> bytes:
    dest = tmp / "d.parquet"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM read_csv('{csv}', header=true)) TO '{dest}'")
    con.close()
    return dest.read_bytes()


@pytest.fixture
def stores(tmp_path: Path) -> tuple[LocalStore, LocalStore]:
    gold = LocalStore(tmp_path / "gold_ready")
    for src, batch in [("hcp_population", "20240101"), ("hcp_population", "20240201"),
                       ("world_bank_gdp", "20240101")]:  # fmt: skip
        _promote(gold, src, batch)
    return gold, LocalStore(tmp_path / "out")


def _rows(out: LocalStore, key: str) -> list[tuple]:  # type: ignore[type-arg]
    return (
        duckdb.connect().execute("SELECT * FROM read_parquet(?) ", [str(out.root / key)]).fetchall()
    )


def test_population_by_region_dedups_latest_batch(stores) -> None:  # type: ignore[no-untyped-def]
    gold, out = stores
    b = build_product(PRODUCTS["population_by_region"], gold, out)
    assert b.rows == 6
    assert _rows(out, b.data_key) == [
        ("A", 2020, 100), ("B", 2020, 300), ("A", 2021, 120),  # batch 0201 supersedes 110
        ("B", 2021, 330), ("A", 2022, 130), ("B", 2022, 370),
    ]  # fmt: skip


def test_summary_gdp_per_capita(stores) -> None:  # type: ignore[no-untyped-def]
    gold, out = stores
    b = build_product(PRODUCTS["socioeconomic_summary"], gold, out)
    got = {(r[1], r[2]): r for r in _rows(out, b.data_key)}
    assert len(got) == 6
    # national population 400/450/500, GDP 4000/9000/15000 -> 10/20/30 per capita
    assert got[("A", 2020)] == ("MAR", "A", 2020, 100, 4000.0, 10.0)
    assert got[("B", 2021)] == ("MAR", "B", 2021, 330, 9000.0, 20.0)
    assert got[("A", 2022)][5] == 30.0


def test_output_schema_matches_contract(stores) -> None:  # type: ignore[no-untyped-def]
    gold, out = stores
    for built in build_all(gold, out, list(PRODUCTS.values())):
        p = PRODUCTS[built.product]
        desc = (
            duckdb.connect()
            .execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(out.root / built.data_key)])
            .fetchall()
        )
        assert [(r[0], r[1]) for r in desc] == [(c.name, DUCKDB_TYPES[c.type]) for c in p.columns]


def test_indicators(stores) -> None:  # type: ignore[no-untyped-def]
    gold, _ = stores
    cols, rows = run_indicator("population_growth", gold)
    g = {(r[0], r[1]): r[3] for r in rows}
    assert cols == ["region", "year", "population_total", "growth_rate"]
    assert g[("A", 2020)] is None
    assert g[("A", 2021)] == pytest.approx(0.2)
    assert g[("B", 2022)] == pytest.approx(370 / 330 - 1)
    _, rows = run_indicator("regional_share", gold)
    s = {(r[0], r[1]): r[3] for r in rows}
    assert s[("A", 2020)] == pytest.approx(0.25)
    assert s[("A", 2021)] == pytest.approx(120 / 450)
    _, rows = run_indicator("avg_unemployment", gold)
    assert rows == [(2020, 15.0), (2021, 18.0), (2022, 21.0)]  # 2021 uses corrected 14.0


def test_rebuild_idempotent(stores) -> None:  # type: ignore[no-untyped-def]
    gold, out = stores
    first = build_all(gold, out, list(PRODUCTS.values()))
    keys = out.list()
    again = build_all(gold, out, list(PRODUCTS.values()))
    assert [b.content_hash for b in first] == [b.content_hash for b in again]
    assert not any(b.created for b in again)
    assert out.list() == keys
    mf = json.loads(out.get(first[0].manifest_key))
    assert mf["sha256"] == first[0].content_hash and "built_at" in mf


def test_redundant_batch_same_product(stores) -> None:  # type: ignore[no-untyped-def]
    gold, out = stores
    a = build_product(PRODUCTS["gdp_indicators"], gold, out)
    gold.put_if_absent("gold_ready/world_bank_gdp/20240301/data.parquet",
                       gold.get("gold_ready/world_bank_gdp/20240101/data.parquet"))  # fmt: skip
    gold.put_if_absent("gold_ready/world_bank_gdp/20240301/manifest.json", b"{}")
    b = build_product(PRODUCTS["gdp_indicators"], gold, out)
    assert a.content_hash == b.content_hash  # same rows after dedup -> same product


def test_no_promoted_batch_is_domain_error(tmp_path: Path) -> None:
    with pytest.raises(GoldError, match="no promoted batch"):
        build_product(PRODUCTS["gdp_indicators"], LocalStore(tmp_path / "g"),
                      LocalStore(tmp_path / "o"))  # fmt: skip


def test_names_and_sql_are_guarded(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    gold, _ = stores
    with pytest.raises(GoldError, match="invalid name"):
        run_indicator("../population_growth", gold)
    with pytest.raises(GoldError, match="no SQL file"):
        run_indicator("nope", gold)
    sql = tmp_path / "gdp_indicators.sql"
    sql.write_text("SELECT * FROM read_csv('/etc/hosts')")
    with pytest.raises(GoldError):
        build_product(PRODUCTS["gdp_indicators"], stores[0], stores[1], sql_dir=tmp_path)
    sql.write_text("DROP TABLE src_world_bank_gdp")
    with pytest.raises(GoldError, match="single SELECT"):
        build_product(PRODUCTS["gdp_indicators"], stores[0], stores[1], sql_dir=tmp_path)


def test_cli_gold_build(stores, monkeypatch, tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    gold, out = stores
    code = main(["gold", "build", "gdp_indicators", "--gold-dir", str(gold.root),
                 "--out-dir", str(out.root)])  # fmt: skip
    assert code == 0 and "gdp_indicators" in capsys.readouterr().out
    assert main(["gold", "build", "nope", "--gold-dir", str(gold.root),
                 "--out-dir", str(out.root)]) == 2  # fmt: skip
