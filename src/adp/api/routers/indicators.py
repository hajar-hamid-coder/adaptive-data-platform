"""Gold indicators (P6-05): paginated, computed from Silver or served from an approved
materialised artefact (same rows, see adp.optimizer.apply)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam

from adp.api.deps import PRODUCT_RE, DepsT, Page, Paging, note_usage
from adp.gold.views import SQL_DIR, indicator_sql
from adp.optimizer.apply import serve_indicator
from adp.optimizer.usage import QueryShape, depth_bucket
from adp.orchestration.promotion_gate import GOLD_PREFIX

router = APIRouter(prefix="/indicators", tags=["indicators"])
Name = Annotated[str, PathParam(pattern=PRODUCT_RE, max_length=64)]


@router.get("", summary="Liste des indicateurs / List indicators")
def list_indicators(page: Paging) -> Page:
    return page.apply(sorted(p.stem for p in (SQL_DIR / "indicators").glob("*.sql")))


@router.get("/{name}", summary="Indicateur paginé / Paginated indicator")
def indicator(
    name: Name,
    request: Request,
    deps: DepsT,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page:
    if not (SQL_DIR / "indicators" / f"{name}.sql").is_file():
        raise HTTPException(404, "Not found")
    _, sources = indicator_sql(name)
    if not all(deps.gold.list(f"{GOLD_PREFIX}/{s}/") for s in sources):
        raise HTTPException(404, "No promoted data")
    cols, rows, total, _ = serve_indicator(deps.gold, name, limit, offset)
    shape = QueryShape(op="indicator", target=name, depth=depth_bucket(offset), limit=limit)
    note_usage(request, shape, len(rows), total, len(cols))
    items = [dict(zip(cols, r, strict=True)) for r in rows]
    return Page(items=items, total=total, limit=limit, offset=offset)
