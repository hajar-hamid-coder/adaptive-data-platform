"""Run audit journal (P6-01), read-only: runs and their steps. Counts, ids and codes only."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query

from adp.api.deps import RUN_ID_RE, SOURCE_RE, DepsT, Page, Paging

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/runs", summary="Journal des runs / Run audit journal")
def audit_runs(
    deps: DepsT, page: Paging,
    source: Annotated[str | None, Query(pattern=SOURCE_RE)] = None,
) -> Page:  # fmt: skip
    rows = deps.audit.runs(source, limit=1000) if deps.audit else []
    return page.apply([_json(r) for r in rows])


@router.get("/runs/{run_id}/steps", summary="Étapes d'un run / Steps of a run")
def audit_steps(
    run_id: Annotated[str, Path(pattern=RUN_ID_RE)], deps: DepsT, page: Paging,
    source: Annotated[str | None, Query(pattern=SOURCE_RE)] = None,
) -> Page:  # fmt: skip
    rows = deps.audit.steps(run_id, source) if deps.audit else []
    if not rows:
        raise HTTPException(404, "Run not found")
    return page.apply([_json(r) for r in rows])


def _json(row: dict[str, object]) -> dict[str, object]:
    return {k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in row.items()}
