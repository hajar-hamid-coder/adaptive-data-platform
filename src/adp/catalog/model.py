"""Catalog models (strict, frozen). No data value ever appears here: names, versions, batch ids."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LineageNode(_Strict):
    """One hop of a lineage path. `column` is None when the raw column is not derivable."""

    layer: Literal["gold", "silver", "bronze"]
    dataset: str  # product (gold) or source (silver, bronze)
    column: str | None
    version: str | None = None  # contract version (gold) or schema_version (silver)
    batches: tuple[str, ...] = ()  # silver batch hashes / bronze parquet paths, sorted


class LineagePath(_Strict):
    """Gold column -> upstream product(s) -> Silver column -> raw Bronze column."""

    nodes: tuple[LineageNode, ...]


class ColumnEntry(_Strict):
    name: str
    type: str
    description: str
    lineage: tuple[LineagePath, ...]
    lineage_confidence: Literal["full", "partial"]
    lineage_notes: tuple[str, ...] = ()  # short codes explaining a partial lineage


class ProductEntry(_Strict):
    name: str
    version: str
    owner: str
    owner_role: str
    criticality: str
    sla_hours: int
    description: str
    columns: tuple[ColumnEntry, ...]
    consumers: tuple[str, ...]
    sources: tuple[str, ...]  # direct upstream sources
    upstream: tuple[str, ...]  # direct upstream products


class Catalog(_Strict):
    products: tuple[ProductEntry, ...]

    def product(self, name: str) -> ProductEntry | None:
        return next((p for p in self.products if p.name == name), None)

    def column(self, product: str, column: str) -> ColumnEntry | None:
        p = self.product(product)
        return next((c for c in p.columns if c.name == column), None) if p else None
