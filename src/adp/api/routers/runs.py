"""Runs, as far as persisted: one entry per (source, run_id) quality score."""

from fastapi import APIRouter

from adp.api.deps import DepsT, Page, Paging

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("", summary="Runs connus (déduits des scores qualité) / Known runs")
def runs(deps: DepsT, page: Paging) -> Page:
    keys = ("source_name", "run_id", "schema_version", "score", "rows_total", "rows_failed",
            "created_at")  # fmt: skip
    rows = [r.model_dump(mode="json", include=set(keys)) for r in deps.scores.batch_scores()]
    return page.apply(rows)
