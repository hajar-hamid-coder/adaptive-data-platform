"""Data products: contracts, latest Gold version and Gold rows (read-only)."""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam

from adp.api.deps import PRODUCT_RE, DepsT, Page, Paging, note_usage
from adp.gold.contract import impact_matrix
from adp.gold.views import latest_gold
from adp.optimizer.apply import serve_product
from adp.optimizer.usage import QueryShape, depth_bucket

router = APIRouter(prefix="/products", tags=["products"])
Name = Annotated[str, PathParam(pattern=PRODUCT_RE, max_length=64)]


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
    latest = latest_gold(deps.gold, name)
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
    request: Request,
    deps: DepsT,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page:
    _find(deps, name)
    latest = latest_gold(deps.gold, name)
    if latest is None:
        raise HTTPException(404, "No Gold version built")
    cols, rows, total, _ = serve_product(deps.gold, name, latest[0], latest[1], limit, offset)
    shape = QueryShape(op="page", target=name, depth=depth_bucket(offset), limit=limit)
    note_usage(request, shape, len(rows), total, len(cols))
    items = [dict(zip(cols, r, strict=True)) for r in rows]
    return Page(items=items, total=total, limit=limit, offset=offset)
