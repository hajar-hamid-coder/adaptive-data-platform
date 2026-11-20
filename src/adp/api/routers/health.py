"""Public liveness endpoint."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness / santé du service")
def health() -> dict[str, str]:
    return {"status": "ok"}
