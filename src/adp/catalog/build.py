"""Build the catalog from persisted metadata only (nothing is re-executed).

Derivation rule of a Gold column's lineage:
1. The product SQL (`sql/gold/<name>.sql`) is PARSED (never executed) with DuckDB's
   `json_serialize_sql`. Each output column of the final SELECT is traced to the base-table
   columns it reads, through CTEs, sub-selects and aliases. `src_<source>` is a Silver column of
   that source, `prod_<product>` a column of an upstream product (traced recursively). Filters,
   joins, window/QUALIFY and ORDER BY do not create column lineage.
2. Unresolved case (unparsable SQL, `*`, sub-query in a select item, UNION, ambiguous unqualified
   column, unknown table): the column is `partial`; if no input was found at all, the canonical
   name is looked up identically in the sources then upstream products (`name_fallback`).
3. Silver -> Bronze: the Silver manifests of the promoted batches (`gold_ready/<source>/*`) give
   the schema_version and the Bronze batch; the raw column(s) come from the mapping journal
   (last decision per source column, applied only if `auto_apply`/`approve`). Without journal
   entry or Bronze batch the lineage stays `partial` instead of being guessed.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import duckdb

from adp.bronze.store import ObjectStore
from adp.catalog.model import Catalog, ColumnEntry, LineageNode, LineagePath, ProductEntry
from adp.gold.contract import DICTIONARY, DataProduct
from adp.gold.views import SQL_DIR
from adp.mapping.decision import LogEntry
from adp.mapping.dictionary import SemanticDictionary, load_dictionary
from adp.orchestration.promotion_gate import GOLD_PREFIX

Leaf = tuple[str, str]  # (base table, column)
APPLIED = ("auto_apply", "approve")


@dataclass(frozen=True)
class Evidence:
    """Persisted metadata read by the catalog; every part is optional (missing = partial)."""

    gold_ready: ObjectStore | None = None
    silver: ObjectStore | None = None
    journal: Sequence[LogEntry] = ()


# --- SQL analysis (parse only) -----------------------------------------------------------
@dataclass(frozen=True)
class _Out:
    leaves: frozenset[Leaf]
    partial: bool


class _Unsupported(Exception):
    pass


_Scope = dict[str, tuple[str | None, dict[str, _Out] | None]]  # alias -> (base table, outputs)


def _refs(expr: Any) -> tuple[list[list[str]], bool]:
    refs: list[list[str]] = []
    partial = False

    def walk(x: Any) -> None:
        nonlocal partial
        if isinstance(x, dict):
            if x.get("class") == "COLUMN_REF":
                refs.append(x["column_names"])
                return
            if x.get("class") in ("SUBQUERY", "STAR"):
                partial = True
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(expr)
    return refs, partial


def _from(node: Any, ctes: dict[str, dict[str, _Out]], scope: _Scope) -> None:
    t = node.get("type") if node else "EMPTY"
    if t == "EMPTY":
        return
    if t == "BASE_TABLE":
        name = node["table_name"]
        key = node["alias"] or name
        scope[key] = (None, ctes[name]) if name in ctes else (name, None)
    elif t == "JOIN":
        _from(node["left"], ctes, scope)
        _from(node["right"], ctes, scope)
    elif t == "SUBQUERY":
        scope[node["alias"]] = (None, _select(node["subquery"]["node"], ctes))
    else:
        raise _Unsupported(t)


def _select(node: Any, outer: dict[str, dict[str, _Out]]) -> dict[str, _Out]:
    if node["type"] != "SELECT_NODE":
        raise _Unsupported(node["type"])
    ctes = dict(outer)
    for item in node["cte_map"]["map"]:
        ctes[item["key"]] = _select(item["value"]["query_node"], ctes)
    scope: _Scope = {}
    _from(node["from_table"], ctes, scope)
    out: dict[str, _Out] = {}
    for item in node["select_list"]:
        refs, partial = _refs(item)
        name = item["alias"] or (item["column_names"][-1] if item["class"] == "COLUMN_REF" else "")
        leaves: set[Leaf] = set()
        for names in refs:
            entry = scope.get(names[0]) if len(names) > 1 else None
            if len(names) == 1 and len(scope) == 1:
                entry = next(iter(scope.values()))
            if entry is None:
                partial = True
            elif entry[0] is not None:
                leaves.add((entry[0], names[-1]))
            else:
                src = (entry[1] or {}).get(names[-1])
                if src is None:
                    partial = True
                else:
                    leaves |= src.leaves
                    partial = partial or src.partial
        if name:
            out[name] = _Out(frozenset(leaves), partial)
    return out


def trace_sql(sql: str) -> dict[str, _Out] | None:
    """Output column -> base columns read; None when the SQL cannot be analysed (parse only)."""
    try:
        raw = duckdb.connect().execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
        doc = json.loads(raw[0]) if raw else {}
        if doc.get("error"):
            return None
        return _select(doc["statements"][0]["node"], {})
    except (_Unsupported, KeyError, IndexError, duckdb.Error, ValueError):
        return None


# --- persisted evidence --------------------------------------------------------------------
def _applied_mapping(journal: Iterable[LogEntry]) -> dict[tuple[str, str], str]:
    """(source, raw column) -> canonical target, from the LAST journal decision per column."""
    last: dict[tuple[str, str], LogEntry] = {}
    for e in journal:
        last[(e.source_name, e.source_col)] = e
    return {k: e.target for k, e in last.items() if e.action in APPLIED and e.target}


def _silver_batches(ev: Evidence, source: str) -> list[dict[str, Any]]:
    """Silver manifests of the promoted batches of a source."""
    if ev.gold_ready is None or ev.silver is None:
        return []
    out = []
    for key in sorted(ev.gold_ready.list(f"{GOLD_PREFIX}/{source}/")):
        if not key.endswith("/manifest.json"):
            continue
        sp = json.loads(ev.gold_ready.get(key)).get("silver_path")
        mf = f"{sp.removesuffix('.parquet')}.manifest.json" if isinstance(sp, str) else ""
        if mf and ev.silver.exists(mf):
            out.append(json.loads(ev.silver.get(mf)))
    return out


def _source_paths(
    ev: Evidence, mapping: Mapping[tuple[str, str], str], source: str, col: str
) -> tuple[list[tuple[LineageNode, LineageNode]], list[str]]:
    """Silver (source, canonical col) -> Bronze raw column(s)."""
    notes: list[str] = []
    mfs = [m for m in _silver_batches(ev, source) if col in m["columns"]]
    if not mfs:
        notes.append("no_silver_evidence")
    versions = sorted({m["schema_version"] for m in mfs})
    silver = LineageNode(
        layer="silver", dataset=source, column=col,
        version=str(versions[-1]) if versions else None,
        batches=tuple(sorted({m["batch_hash"] for m in mfs})),
    )  # fmt: skip
    bronze_batches = tuple(sorted({m["bronze_batch"] for m in mfs if m.get("bronze_batch")}))
    if mfs and not bronze_batches:
        notes.append("no_bronze_batch")
    raws = sorted(c for (s, c), t in mapping.items() if s == source and t == col)
    if not raws:
        notes.append("no_mapping_decision")
        raws = [None]  # type: ignore[list-item]
    return [
        (silver, LineageNode(layer="bronze", dataset=source, column=r, batches=bronze_batches))
        for r in raws
    ], notes


# --- build -------------------------------------------------------------------------------
def _product_lineage(
    p: DataProduct, col: str, products: Mapping[str, DataProduct], ev: Evidence,
    mapping: Mapping[tuple[str, str], str], sql_dir: Path,
) -> tuple[list[LineagePath], list[str]]:  # fmt: skip
    me = LineageNode(layer="gold", dataset=p.name, column=col, version=p.version)
    notes: list[str] = []
    sql_file = sql_dir / f"{p.name}.sql"
    traced = trace_sql(sql_file.read_text(encoding="utf-8")) if sql_file.is_file() else None
    if traced is None:
        notes.append("sql_missing" if not sql_file.is_file() else "sql_unparsed")
    out = traced.get(col) if traced else None
    if traced is not None and out is None:
        notes.append("sql_column_not_found")
    leaves = set(out.leaves) if out else set()
    if out is None or out.partial:
        notes.append("unresolved_ref")
    if not leaves:  # canonical-name fallback (never silent: flagged partial)
        notes.append("name_fallback")
        leaves = {(f"src_{s}", col) for s in p.sources} | {(f"prod_{d}", col) for d in p.depends_on}
        leaves = {
            (t, c) for t, c in leaves
            if t.startswith("src_") or col in {x.name for x in products[t[5:]].columns}
        }  # fmt: skip
    paths: list[LineagePath] = []
    for table, c in sorted(leaves):
        if table.startswith("prod_") and table[5:] in products:
            sub, sub_notes = _product_lineage(
                products[table[5:]], c, products, ev, mapping, sql_dir
            )
            paths += [LineagePath(nodes=(me, *s.nodes)) for s in sub]
            notes += sub_notes
        elif table.startswith("src_"):
            pairs, sub_notes = _source_paths(ev, mapping, table[4:], c)
            paths += [LineagePath(nodes=(me, s, b)) for s, b in pairs]
            notes += sub_notes
        else:
            notes.append("unknown_table")
    if not paths:
        paths = [LineagePath(nodes=(me,))]
    return paths, notes


def build_catalog(
    products: Iterable[DataProduct],
    *,
    evidence: Evidence = Evidence(),  # noqa: B008
    sql_dir: Path = SQL_DIR,
    dictionary: SemanticDictionary | None = None,
) -> Catalog:
    """Catalog of the contracts with column-level lineage; deterministic (sorted)."""
    by_name = {p.name: p for p in products}
    dictionary = dictionary or load_dictionary(DICTIONARY)
    desc = {f.name: f.description for f in dictionary.fields}
    mapping = _applied_mapping(evidence.journal)
    entries = []
    for p in sorted(by_name.values(), key=lambda p: p.name):
        cols = []
        for c in p.columns:
            paths, notes = _product_lineage(p, c.name, by_name, evidence, mapping, sql_dir)
            uniq = tuple(sorted(set(notes)))
            conf: Literal["full", "partial"] = "partial" if uniq else "full"
            cols.append(ColumnEntry(
                name=c.name, type=c.type, description=desc.get(c.name, ""),
                lineage=tuple(sorted(set(paths), key=lambda x: x.model_dump_json())),
                lineage_confidence=conf, lineage_notes=uniq,
            ))  # fmt: skip
        entries.append(ProductEntry(
            name=p.name, version=p.version, owner=p.owner.team, owner_role=p.owner.role,
            criticality=p.criticality.value, sla_hours=p.sla.freshness_hours,
            description=p.description, columns=tuple(cols),
            consumers=tuple(sorted(x.name for x in p.consumers)),
            sources=tuple(sorted(p.sources)), upstream=tuple(sorted(p.depends_on)),
        ))  # fmt: skip
    return Catalog(products=tuple(entries))
