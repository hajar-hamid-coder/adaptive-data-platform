"""Quality: batch scores, per-run JSON reports and quarantine (indices and reasons only)."""

import json
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi import Path as PathParam

from adp.api.deps import RUN_ID_RE, SOURCE_RE, DepsT, Page, Paging

router = APIRouter(prefix="/quality", tags=["quality"])


@router.get("/scores", summary="Scores qualité par lot / Batch quality scores")
def scores(
    deps: DepsT,
    page: Paging,
    source: Annotated[str | None, Query(pattern=SOURCE_RE)] = None,
    run_id: Annotated[str | None, Query(pattern=RUN_ID_RE)] = None,
) -> Page:
    rows = deps.scores.batch_scores(source, run_id)
    return page.apply([r.model_dump(mode="json") for r in rows])


@router.get("/reports/{run_id}", summary="Rapport qualité JSON d'un run")
def report(run_id: Annotated[str, PathParam(pattern=RUN_ID_RE)], deps: DepsT) -> dict[str, Any]:
    # the promotion gate writes <reports_dir>/<source>/<run_id>.json; run_id is regex-validated
    path = next((p for p in sorted(deps.reports_dir.glob(f"*/{run_id}.json")) if p.is_file()), None)
    if path is None:
        raise HTTPException(404, "Not found")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


@router.get("/quarantine/{source}", summary="Lignes en quarantaine (sans valeurs brutes)")
def quarantine(
    source: Annotated[str, PathParam(pattern=SOURCE_RE)], deps: DepsT, page: Paging
) -> Page:
    items: list[dict[str, Any]] = []
    for batch in sorted(deps.quarantine.list_batches(source)):
        st = deps.quarantine.state(source, batch)
        items += [
            {
                "batch_id": batch, "row": r.row, "status": r.status, "batch_level": st.level,
                "reasons": [{"rule_id": a, "severity": b} for a, b in r.reasons],
            }
            for r in st.rows
        ]  # fmt: skip
    return page.apply(items)
