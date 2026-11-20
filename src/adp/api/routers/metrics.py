"""Prometheus scrape endpoint (P6-01), text exposition format."""

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["observability"])


@router.get("/metrics", summary="Métriques Prometheus / Prometheus metrics",
            response_class=Response)  # fmt: skip
def metrics(request: Request) -> Response:
    return Response(generate_latest(request.app.state.metrics.registry),
                    media_type=CONTENT_TYPE_LATEST)  # fmt: skip
