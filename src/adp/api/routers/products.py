"""Data products: contracts, latest Gold version and Gold rows (read-only)."""

import json
import tempfile
from pathlib import Path
from typing import Annotated, Any

import duckdb
from fastapi import APIRouter, HTTPException, Query
from fastapi import Path as PathParam

from adp.api.deps import PRODUCT_RE, DepsT, Page, Paging
from adp.gold.contract import impact_matrix
from adp.gold.views import OUT_PREFIX

router = APIRouter(prefix="/products", tags=["products"])
Name = Annotated[str, PathParam(pattern=PRODUCT_RE, max_length=64)]


def _latest(deps: Any, name: str) -> tuple[dict[str, Any], str] | None:
    """Newest manifest (highest contract version, then build date) and its data key."""
    best: tuple[tuple[Any, ...], dict[str, Any], str] | None = None
    for key in deps.gold.list(f"{OUT_PREFIX}/{name}/"):
        if not key.endswith("/manifest.json"):
            continue
        m = json.loads(deps.gold.get(key))
        rank = (tuple(int(x) for x in m["contract_version"].split(".")), m["built_at"])
        if best is None or rank > best[0]:
            best = (rank, m, key.removesuffix("manifest.json") + "data.parquet")
    return (best[1], best[2]) if best else None


def _find(deps: Any, name: str) -> Any:
    p = deps.products.get(name)
    if p is None:
        raise HTTPException(404, "Not found")
    return p


@router.get("", summary="Liste des data products / List data products")
def list_products(deps: DepsT, page: Paging) -> Page:
    rows = [
        {
            "name": p.name, "version": p.version, "description": p.description,
            "criticality": p.criticality.value, "owner_team": p.owner.team,
            "freshness_hours": p.sla.freshness_hours,
        }
        for p in sorted(deps.products.values(), key=lambda p: p.name)
    ]  # fmt: skip
    return page.apply(rows)


@router.get("/{name}", summary="Contrat, SLA et dernière version Gold")
def get_product(name: Name, deps: DepsT) -> dict[str, Any]:
    p = _find(deps, name)
    eff = next(r for r in impact_matrix(deps.products.values()).products if r.name == name)
    latest = _latest(deps, name)
    gold = None
    if latest:
        m = latest[0]
        gold = {k: m[k] for k in ("contract_version", "rows", "sha256", "built_at")}
    return {
        "contract": p.model_dump(mode="json"),
        "effective_criticality": eff.effective_criticality.value,
        "latest_gold": gold,
    }


@router.get("/{name}/data", summary="Lignes Gold paginées / Paginated Gold rows")
def product_data(
    name: Name,
    deps: DepsT,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page:
    _find(deps, name)
    latest = _latest(deps, name)
    if latest is None:
        raise HTTPException(404, "No Gold version built")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "d.parquet"
        f.write_bytes(deps.gold.get(latest[1]))
        con = duckdb.connect()
        try:
            total = con.execute("SELECT count(*) FROM read_parquet(?)", [str(f)]).fetchone()
            cur = con.execute(
                "SELECT * FROM read_parquet(?) LIMIT ? OFFSET ?", [str(f), limit, offset]
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        finally:
            con.close()
    return Page(items=rows, total=total[0] if total else 0, limit=limit, offset=offset)
