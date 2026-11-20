"""Schema registry: versions of a source and the changes between two versions."""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi import Path as PathParam

from adp.api.deps import SOURCE_RE, DepsT, Page, Paging

router = APIRouter(prefix="/schemas", tags=["schemas"])
Source = Annotated[str, PathParam(pattern=SOURCE_RE)]


@router.get("/{source}", summary="Versions du schéma d'une source")
def schema_versions(source: Source, deps: DepsT, page: Paging) -> Page:
    hist = deps.registry.history(source)
    if not hist:
        raise HTTPException(404, "Not found")
    return page.apply([v.model_dump(mode="json") for v in hist])


@router.get("/{source}/diff", summary="Changements entre deux versions / Diff between versions")
def schema_diff(
    source: Source,
    deps: DepsT,
    page: Paging,
    from_: Annotated[int, Query(alias="from", ge=1)],
    to: Annotated[int, Query(ge=1)],
) -> Page:
    if deps.registry.get(source, from_) is None or deps.registry.get(source, to) is None:
        raise HTTPException(404, "Not found")
    changes: list[dict[str, Any]] = [
        r.change.model_dump(mode="json")
        for r in deps.registry.changes(source, to)
        if r.from_version == from_
    ]
    return page.apply(changes)
