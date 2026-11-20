"""Data catalog and column lineage (read-only): product metadata, owners, lineage to Bronze."""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi import Path as PathParam

from adp.api.deps import PRODUCT_RE, DepsT, Page, Paging
from adp.catalog.build import build_catalog
from adp.catalog.model import Catalog

router = APIRouter(prefix="/catalog", tags=["catalog"])
Name = Annotated[str, PathParam(pattern=PRODUCT_RE, max_length=64)]
ProductQ = Annotated[str, Query(pattern=PRODUCT_RE, max_length=64)]
ColumnQ = Annotated[str, Query(pattern=PRODUCT_RE, max_length=64)]


def _catalog(deps: Any) -> Catalog:
    """Injected factory (stores + journal), else contracts only (lineage partial)."""
    return deps.catalog() if deps.catalog else build_catalog(deps.products.values())


@router.get("", summary="Catalogue des data products / Product catalog")
def list_catalog(deps: DepsT, page: Paging) -> Page:
    rows = [
        p.model_dump(mode="json", exclude={"columns"}) | {"n_columns": len(p.columns)}
        for p in _catalog(deps).products
    ]
    return page.apply(rows)


@router.get("/products/{name}", summary="Produit, colonnes et lignage")
def get_catalog_product(name: Name, deps: DepsT) -> dict[str, Any]:
    p = _catalog(deps).product(name)
    if p is None:
        raise HTTPException(404, "Not found")
    return p.model_dump(mode="json")


@router.get("/lineage", summary="Lignage d'une colonne Gold jusqu'à Bronze")
def column_lineage(product: ProductQ, column: ColumnQ, deps: DepsT) -> dict[str, Any]:
    c = _catalog(deps).column(product, column)
    if c is None:
        raise HTTPException(404, "Not found")
    return c.model_dump(mode="json")
