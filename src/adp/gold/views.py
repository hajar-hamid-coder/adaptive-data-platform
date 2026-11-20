"""Gold views and indicators (P5-02): DuckDB SQL over promoted batches, materialised as Parquet.

Decisions:
- One read-only SELECT per product in `sql/gold/<product>.sql` (indicators: `sql/gold/indicators/`),
  read from that directory only; names are validated identifiers (no path traversal).
- Inputs are in-memory tables: `src_<source>` = union_by_name of every promoted batch
  `gold_ready/<source>/*/data.parquet` (with a `filename` column, batch order = id order) and
  `prod_<product>` for upstream products (built on demand, idempotent). The SQL dedups on the
  natural key, the most recent batch winning. The query then runs with external access disabled
  and configuration locked, so SQL cannot touch files or the network.
- Output columns must equal the contract (names, order); types come from the contract and the
  Parquet is written by a second connection, so the bytes depend only on the data.
- Artefacts are immutable and content-addressed: `gold/<product>/v<contract>/<sha12>/data.parquet`
  then `manifest.json` (`put_if_absent`; build date lives only in the manifest, outside the hash).
  Rebuilding with the same inputs writes nothing new. Older versions are kept.
- No promoted batch for a needed source -> `GoldError` (an empty product would hide an outage).
"""

import hashlib
import json
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from adp.bronze.store import ObjectStore
from adp.errors import GoldError
from adp.gold.contract import DataProduct, load_products
from adp.logging import get_logger
from adp.orchestration.promotion_gate import GOLD_PREFIX
from adp.silver.writer import DUCKDB_TYPES

log = get_logger(__name__)

SQL_DIR = Path(__file__).parents[3] / "sql" / "gold"
OUT_PREFIX = "gold"
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_SOURCES = re.compile(r"^--\s*sources:\s*(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class BuiltProduct:
    product: str
    version: str
    content_hash: str
    rows: int
    data_key: str
    manifest_key: str
    created: bool  # False when an identical build already existed


def _ident(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise GoldError(f"invalid name '{name}'")
    return name


def _read_sql(name: str, sql_dir: Path) -> str:
    path = sql_dir / f"{_ident(name)}.sql"
    if not path.is_file():
        raise GoldError(f"no SQL file for '{name}' in {sql_dir}")
    return path.read_text(encoding="utf-8")


def _batches(gold: ObjectStore, source: str) -> list[str]:
    """Promoted batch ids of a source (data and manifest both present), sorted."""
    prefix = f"{GOLD_PREFIX}/{_ident(source)}/"
    keys = set(gold.list(prefix))
    ids = [
        k.split("/")[2] for k in sorted(keys)
        if k.endswith("/data.parquet") and k.removesuffix("data.parquet") + "manifest.json" in keys
    ]  # fmt: skip
    if not ids:
        raise GoldError(f"no promoted batch for source '{source}' in {prefix}")
    return ids


def _load_source(
    con: duckdb.DuckDBPyConnection, gold: ObjectStore, source: str, tmp: Path
) -> list[str]:
    ids = _batches(gold, source)
    files = []
    for b in ids:
        f = tmp / source / f"{b}.parquet"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(gold.get(f"{GOLD_PREFIX}/{source}/{b}/data.parquet"))
        files.append(str(f))
    con.execute(
        f"CREATE TABLE src_{source} AS "
        "SELECT * FROM read_parquet(?, union_by_name=true, filename=true)",
        [files],
    )
    return ids


def _run_select(
    con: duckdb.DuckDBPyConnection, sql: str
) -> tuple[list[str], list[tuple[Any, ...]]]:
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")
    stmts = con.extract_statements(sql)
    if len(stmts) != 1 or stmts[0].type != duckdb.StatementType.SELECT:
        raise GoldError("Gold SQL must be a single SELECT statement")
    try:
        cur = con.execute(sql)
        return [d[0] for d in cur.description], cur.fetchall()
    except duckdb.Error as exc:
        raise GoldError(f"Gold SQL failed: {type(exc).__name__}: {exc}") from exc


def _to_parquet(product: DataProduct, rows: list[tuple[Any, ...]]) -> bytes:
    ddl = ", ".join(f'"{c.name}" {DUCKDB_TYPES[c.type]}' for c in product.columns)
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "out.parquet"
        con = duckdb.connect()
        try:
            con.execute(f"CREATE TABLE t ({ddl})")
            if rows:
                marks = ",".join("?" * len(product.columns))
                con.executemany(f"INSERT INTO t VALUES ({marks})", rows)
            con.execute(f"COPY t TO '{dest}' (FORMAT PARQUET)")
        finally:
            con.close()
        return dest.read_bytes()


def build_product(
    product: DataProduct,
    gold_store: ObjectStore,
    out_store: ObjectStore,
    catalog: Mapping[str, DataProduct] | None = None,
    *,
    sql_dir: Path = SQL_DIR,
) -> BuiltProduct:
    """Build (idempotently) one data product from promoted batches and upstream products."""
    if catalog is None:
        catalog = {p.name: p for p in load_products()} if product.depends_on else {}
    sql = _read_sql(product.name, sql_dir)
    ups = {d: build_product(_catalog_get(catalog, d), gold_store, out_store, catalog,
                            sql_dir=sql_dir) for d in product.depends_on}  # fmt: skip
    with tempfile.TemporaryDirectory() as tmp:
        con = duckdb.connect()
        try:
            batches = {s: _load_source(con, gold_store, s, Path(tmp)) for s in product.sources}
            for d, built in ups.items():
                f = Path(tmp) / f"{_ident(d)}.parquet"
                f.write_bytes(out_store.get(built.data_key))
                con.execute(f"CREATE TABLE prod_{d} AS SELECT * FROM read_parquet(?)", [str(f)])
            cols, rows = _run_select(con, sql)
        finally:
            con.close()
    expected = [c.name for c in product.columns]
    if cols != expected:
        raise GoldError(f"'{product.name}': output columns {cols} != contract {expected}")
    data = _to_parquet(product, rows)
    digest = hashlib.sha256(data).hexdigest()
    base = f"{OUT_PREFIX}/{product.name}/v{product.version}/{digest[:12]}"
    data_key, mf_key = f"{base}/data.parquet", f"{base}/manifest.json"
    out_store.put_if_absent(data_key, data)
    manifest = {
        "product": product.name, "contract_version": product.version, "rows": len(rows),
        "sha256": digest, "data": data_key, "source_batches": batches,
        "upstream_products": {d: b.content_hash for d, b in ups.items()},
        "built_at": datetime.now(UTC).isoformat(),
    }  # fmt: skip
    created = out_store.put_if_absent(mf_key, json.dumps(manifest, indent=2).encode())
    log.info("gold_build", product=product.name, rows=len(rows), sha=digest[:12], created=created)
    return BuiltProduct(product.name, product.version, digest, len(rows), data_key, mf_key, created)


def _catalog_get(catalog: Mapping[str, DataProduct], name: str) -> DataProduct:
    if name not in catalog:
        raise GoldError(f"unknown upstream product '{name}'")
    return catalog[name]


def build_all(
    gold_store: ObjectStore, out_store: ObjectStore, products: list[DataProduct] | None = None,
    *, sql_dir: Path = SQL_DIR,
) -> list[BuiltProduct]:  # fmt: skip
    """Build every product (upstream ones first through recursion)."""
    products = products if products is not None else load_products()
    catalog = {p.name: p for p in products}
    return [build_product(p, gold_store, out_store, catalog, sql_dir=sql_dir) for p in products]


def run_indicator(
    name: str, gold_store: ObjectStore, *, sql_dir: Path = SQL_DIR
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Compute an indicator (not materialised) from `sql/gold/indicators/<name>.sql`."""
    sql = _read_sql(name, sql_dir / "indicators")
    m = _SOURCES.search(sql)
    sources = [s.strip() for s in m.group(1).split(",")] if m else []
    with tempfile.TemporaryDirectory() as tmp:
        con = duckdb.connect()
        try:
            for s in sources:
                _load_source(con, gold_store, _ident(s), Path(tmp))
            return _run_select(con, sql)
        finally:
            con.close()
