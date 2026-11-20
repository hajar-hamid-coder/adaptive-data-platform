"""Shared fixtures: a store with synthetic promoted batches (seeded, hash-based, no real data)."""

from pathlib import Path

import duckdb
import pytest

from adp.bronze.store import LocalStore
from adp.gold.contract import load_products
from adp.gold.views import build_product

PRODUCTS = {p.name: p for p in load_products()}


def promote(store: LocalStore, batch: str, regions: int, years: int, seed: int = 7) -> None:
    """Synthetic `hcp_population` batch under gold_ready/ (regions x years distinct keys)."""
    dest = store.root.parent / f"{batch}.parquet"
    con = duckdb.connect()
    con.execute(
        f"""COPY (
            SELECT 'R' || lpad(r::VARCHAR, 5, '0') AS region, 1900 + y AS year,
                   (1000 + hash(r, y, {seed}) % 1000000)::BIGINT AS population_total,
                   round((hash(y, r, {seed}) % 2000) / 100.0, 2) AS unemployment_rate
            FROM range({regions}) a(r), range({years}) b(y) ORDER BY y, r
        ) TO '{dest}' (FORMAT PARQUET)"""
    )
    con.close()
    store.put_if_absent(f"gold_ready/hcp_population/{batch}/data.parquet", dest.read_bytes())
    store.put_if_absent(f"gold_ready/hcp_population/{batch}/manifest.json", b"{}")


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    s = LocalStore(tmp_path / "store")
    promote(s, "20240101", regions=60, years=50)  # 3 000 rows
    build_product(PRODUCTS["population_by_region"], s, s)
    return s
