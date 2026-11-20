"""Catalog wired on the configured stores and mapping journal (API default, CLI)."""

from sqlalchemy.exc import SQLAlchemyError

from adp.catalog.build import Evidence, build_catalog
from adp.catalog.model import Catalog
from adp.errors import AdpError
from adp.gold.contract import load_products
from adp.logging import get_logger
from adp.mapping.decision import ReviewQueue
from adp.orchestration.promote_asset import build_gold_store
from adp.orchestration.silver_chain import build_silver_store
from adp.schema.registry import make_engine

log = get_logger(__name__)


def default_catalog() -> Catalog:
    """Read the persisted metadata on each call; an unreachable journal leaves lineage partial."""
    try:
        journal = ReviewQueue(make_engine()).journal()
    except (AdpError, SQLAlchemyError) as exc:
        log.warning("catalog_journal_unavailable", error=type(exc).__name__)
        journal = []
    ev = Evidence(gold_ready=build_gold_store(), silver=build_silver_store(), journal=journal)
    return build_catalog(load_products(), evidence=ev)
