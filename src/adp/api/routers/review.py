"""Mapping review (P6-07): list the queue (viewer+), approve/reject (steward+).

Same `ReviewQueue` functions as the CLI `adp review`: same guards, append-only journal. The
decider (`by`) is the authenticated principal, never read from the request. Responses carry
names, scores and statuses only: no rationale, no data value. Errors are generic.
Replay: repeating the same decision on a decided item returns it unchanged (no new journal
row); the opposite decision answers 409.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from adp.api.deps import SOURCE_RE, DepsT, Page, Paging
from adp.api.rbac import Capability, Principal, require_role
from adp.errors import MappingError
from adp.logging import get_logger
from adp.mapping.decision import ReviewItem, ReviewQueue

log = get_logger(__name__)
router = APIRouter(prefix="/review", tags=["review"])

_read = Depends(require_role(Capability.REVIEW_READ))
_decide = require_role(Capability.REVIEW_DECIDE)
ItemId = Annotated[int, Path(ge=1, le=2**31 - 1)]


class DecisionBody(BaseModel):
    """Optional note only; the decider is the authenticated principal."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=500)


def _view(i: ReviewItem) -> dict[str, object]:
    return i.model_dump(mode="json", include={
        "id", "source_name", "source_col", "target", "final_target", "score", "method",
        "origin", "reason", "status", "created_at", "decided_at", "decided_by", "decision_note",
    })  # fmt: skip


def _queue(deps: DepsT) -> ReviewQueue:
    if deps.review is None:
        raise HTTPException(503, "Service unavailable")
    return deps.review


@router.get("", summary="File de revue / Review queue", dependencies=[_read])
def review_list(
    deps: DepsT, page: Paging,
    status: Annotated[
        Literal["pending", "approved", "rejected", "superseded"] | None, Query()
    ] = None,
    source: Annotated[str | None, Query(pattern=SOURCE_RE)] = None,
) -> Page:  # fmt: skip
    try:
        rows = _queue(deps).list(status=status, source_name=source)
    except MappingError as exc:
        log.error("review_read_failed", error=type(exc).__name__)
        raise HTTPException(500, "Internal error") from exc
    return page.apply([_view(r) for r in rows])


def _decide_item(
    deps: DepsT, item_id: int, who: Principal, body: DecisionBody,
    action: Literal["approve", "reject"],
) -> dict[str, object]:  # fmt: skip
    queue = _queue(deps)
    wanted = "approved" if action == "approve" else "rejected"
    for _ in range(2):  # second pass: lost a race with a concurrent decision
        item = queue.get(item_id)
        if item is None:
            raise HTTPException(404, "Not found")
        if item.status == wanted:
            return _view(item)  # idempotent replay
        if item.status != "pending":
            raise HTTPException(409, "Conflict")
        try:
            done = (queue.approve if action == "approve" else queue.reject)(
                item_id, who.name, note=body.note
            )
            return _view(done)
        except MappingError:
            continue
    raise HTTPException(409, "Conflict")


@router.post("/{item_id}/approve", summary="Approuver un mapping / Approve a mapping")
def review_approve(
    item_id: ItemId, deps: DepsT, who: Annotated[Principal, Depends(_decide)],
    body: DecisionBody | None = None,
) -> dict[str, object]:  # fmt: skip
    return _decide_item(deps, item_id, who, body or DecisionBody(), "approve")


@router.post("/{item_id}/reject", summary="Rejeter un mapping / Reject a mapping")
def review_reject(
    item_id: ItemId, deps: DepsT, who: Annotated[Principal, Depends(_decide)],
    body: DecisionBody | None = None,
) -> dict[str, object]:  # fmt: skip
    return _decide_item(deps, item_id, who, body or DecisionBody(), "reject")
