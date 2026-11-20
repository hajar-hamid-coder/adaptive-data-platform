"""OpenLineage-lite export: one COMPLETE event per job (Silver per source, Gold per product).

Inspired by the OpenLineage spec (job, inputs, outputs, `schema` and `columnLineage` facets) but
trimmed: no eventTime / run id, so the output is deterministic (sorted). Datasets are named
`<layer>.<name>` in namespace `adp`. A column absent from a lineage (partial) is left out.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from adp.catalog.model import Catalog

NAMESPACE = "adp"
PRODUCER = "https://github.com/adp/adaptive-data-platform"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OLField(_Strict):
    name: str
    type: str | None = None


class OLInputField(_Strict):
    namespace: str
    name: str
    field: str


class OLColumn(_Strict):
    inputFields: tuple[OLInputField, ...]  # noqa: N815 (OpenLineage naming)


class OLDataset(_Strict):
    namespace: str
    name: str
    facets: dict[str, Any]


class OLJob(_Strict):
    namespace: str
    name: str


class OLEvent(_Strict):
    eventType: str = "COMPLETE"  # noqa: N815
    producer: str = PRODUCER
    job: OLJob
    inputs: tuple[OLDataset, ...]
    outputs: tuple[OLDataset, ...]


class OLExport(_Strict):
    events: tuple[OLEvent, ...]


def _ds(layer: str, name: str, facets: dict[str, Any] | None = None) -> OLDataset:
    return OLDataset(namespace=NAMESPACE, name=f"{layer}.{name}", facets=facets or {})


def _schema(fields: dict[str, str | None]) -> dict[str, Any]:
    return {
        "schema": {
            "fields": [OLField(name=n, type=t).model_dump() for n, t in sorted(fields.items())]
        }
    }


def build_export(catalog: Catalog) -> OLExport:
    """job (output dataset) -> {output column: {input dataset: input columns}} from the paths."""
    edges: dict[tuple[str, str], dict[str, set[tuple[str, str, str]]]] = {}
    types: dict[tuple[str, str], dict[str, str | None]] = {}
    for p in catalog.products:
        types[("gold", p.name)] = {c.name: c.type for c in p.columns}
        for c in p.columns:
            for path in c.lineage:
                for up, down in zip(path.nodes[1:], path.nodes, strict=False):
                    if up.column is None or down.column is None:
                        continue
                    out = edges.setdefault((down.layer, down.dataset), {})
                    out.setdefault(down.column, set()).add((up.layer, up.dataset, up.column))
                    types.setdefault((down.layer, down.dataset), {}).setdefault(down.column, None)
                    types.setdefault((up.layer, up.dataset), {}).setdefault(up.column, None)
    events = []
    for (layer, name), cols in sorted(edges.items()):
        ins = sorted({(lay, d) for refs in cols.values() for lay, d, _ in refs})
        facets = _schema(types[(layer, name)]) | {
            "columnLineage": {
                "fields": {
                    col: OLColumn(
                        inputFields=tuple(
                            OLInputField(namespace=NAMESPACE, name=f"{lay}.{d}", field=f)
                            for lay, d, f in sorted(refs)
                        )
                    ).model_dump()
                    for col, refs in sorted(cols.items())
                }
            }
        }
        events.append(OLEvent(
            job=OLJob(namespace=NAMESPACE, name=f"{layer}.{name}"),
            inputs=tuple(_ds(lay, d, _schema(types[(lay, d)])) for lay, d in ins),
            outputs=(_ds(layer, name, facets),),
        ))  # fmt: skip
    return OLExport(events=tuple(events))


def export_openlineage(catalog: Catalog) -> dict[str, Any]:
    """Validated, deterministic JSON-able dict."""
    return build_export(catalog).model_dump(mode="json")
