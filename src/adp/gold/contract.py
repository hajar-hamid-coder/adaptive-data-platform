"""Data product contracts (P5-01) and BIA impact matrix.

A data product = one Gold dataset described by `config/products/<name>.yaml`: canonical
columns, upstream sources and products, owner, freshness SLA, criticality, consumers and
expected quality. P5-02/03/04 (DuckDB views, API, catalogue/lineage) reuse `load_products`.

Decisions:
- Structure is checked by strict pydantic models (`DataProduct`); cross-references (canonical
  dictionary, known sources, promotion gate threshold, other products) by `load_product(s)`.
- `quality.min_score` must be >= the promotion gate `min_score`: Gold only holds promoted
  batches, a lower expectation would be meaningless.
- Effective criticality of a product = max(own, own criticality of every transitive downstream
  product); a source gets the max effective criticality of the products built on it.
- Error messages carry file, field path and pydantic message, never the input value.
- Owners and consumers are fictitious teams/roles (H-05): no person name, no email field.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from adp.errors import AdpError, ContractError
from adp.mapping.dictionary import CanonicalType, SemanticDictionary, load_dictionary
from adp.orchestration.promotion_gate import PromotionConfig, load_promotion_config
from adp.sources.spec import load_registry

PRODUCTS_DIR = Path("config/products")
DICTIONARY = Path("config/semantic/canonical.yaml")
SOURCES_DIR = Path("config/sources")
PROMOTION = Path("config/quality/promotion.yaml")

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Criticality(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return list(Criticality).index(self)


_LABEL = {
    Criticality.LOW: "faible",
    Criticality.MEDIUM: "moyenne",
    Criticality.HIGH: "haute",
    Criticality.CRITICAL: "critique",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Column(_Strict):
    name: Identifier
    type: CanonicalType


class Owner(_Strict):
    team: Text
    role: Text


class Sla(_Strict):
    freshness_hours: int = Field(gt=0)


class Consumer(_Strict):
    name: Text
    usage: Text
    dependency: Literal["hard", "soft"]


class Quality(_Strict):
    min_score: float = Field(ge=0, le=100, allow_inf_nan=False)


class DataProduct(_Strict):
    """Contract of one Gold data product (structure only; see `load_product`)."""

    name: Identifier
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: Text
    sources: tuple[Identifier, ...] = ()
    depends_on: tuple[Identifier, ...] = ()
    columns: tuple[Column, ...] = Field(min_length=1)
    owner: Owner
    sla: Sla
    criticality: Criticality
    consumers: tuple[Consumer, ...] = Field(min_length=1)
    quality: Quality
    retention_days: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not self.sources and not self.depends_on:
            raise ValueError("needs at least one upstream source or product")
        if self.name in self.depends_on:
            raise ValueError("a product cannot depend on itself")
        for label, names in (
            ("column", [c.name for c in self.columns]),
            ("consumer", [c.name for c in self.consumers]),
        ):
            dup = sorted({n for n in names if names.count(n) > 1})
            if dup:
                raise ValueError(f"duplicate {label} name(s): {', '.join(dup)}")
        return self


def _check(
    p: DataProduct,
    dictionary: SemanticDictionary,
    known_sources: set[str],
    promotion: PromotionConfig,
) -> list[str]:
    errors: list[str] = []
    canon = {f.name: f for f in dictionary.fields}
    for c in p.columns:
        f = canon.get(c.name)
        if f is None:
            errors.append(f"column '{c.name}' is not a canonical field")
        elif f.type != c.type:
            errors.append(f"column '{c.name}': type {c.type} != canonical {f.type}")
    errors += [f"unknown source '{s}'" for s in p.sources if s not in known_sources]
    if p.quality.min_score < promotion.min_score:
        errors.append(
            f"quality.min_score {p.quality.min_score:g} < promotion gate {promotion.min_score:g}"
        )
    return errors


def load_product(
    path: Path,
    *,
    dictionary: SemanticDictionary | None = None,
    known_sources: set[str] | None = None,
    promotion: PromotionConfig | None = None,
) -> DataProduct:
    """Load and fully validate one contract; raise ContractError naming file and fields."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ContractError(f"{path}: cannot read YAML ({type(exc).__name__})") from exc
    if not isinstance(raw, dict):
        raise ContractError(f"{path}: expected a YAML mapping at top level")
    try:
        product = DataProduct.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"field '{'.'.join(str(x) for x in e['loc']) or '?'}': {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
        )
        raise ContractError(f"{path}: invalid data product contract: {details}") from exc
    if product.name != path.stem:
        raise ContractError(f"{path}: file name must match product name '{product.name}'")
    try:
        dictionary = dictionary or load_dictionary(DICTIONARY)
        if known_sources is None:
            known_sources = set(load_registry(SOURCES_DIR))
        promotion = promotion or load_promotion_config(PROMOTION)
    except AdpError as exc:
        raise ContractError(f"{path}: cannot load reference configuration: {exc}") from exc
    errors = _check(product, dictionary, known_sources, promotion)
    if errors:
        raise ContractError(f"{path}: invalid data product contract: {'; '.join(errors)}")
    return product


def load_products(directory: Path = PRODUCTS_DIR) -> list[DataProduct]:
    """Load every `*.yaml|*.yml` contract; names unique, dependencies known and acyclic."""
    try:
        dictionary = load_dictionary(DICTIONARY)
        known = set(load_registry(SOURCES_DIR))
        promotion = load_promotion_config(PROMOTION)
    except AdpError as exc:
        raise ContractError(f"cannot load reference configuration: {exc}") from exc
    paths = sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")])
    products: dict[str, DataProduct] = {}
    for path in paths:
        p = load_product(path, dictionary=dictionary, known_sources=known, promotion=promotion)
        if p.name in products:
            raise ContractError(f"{path}: duplicate data product name '{p.name}'")
        products[p.name] = p
    for p in products.values():
        for d in p.depends_on:
            if d not in products:
                raise ContractError(f"product '{p.name}': unknown upstream product '{d}'")
    _check_acyclic(products)
    return sorted(products.values(), key=lambda p: p.name)


def _check_acyclic(products: dict[str, DataProduct]) -> None:
    state: dict[str, int] = {}  # 1 = visiting, 2 = done

    def visit(name: str, path: tuple[str, ...]) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            raise ContractError(f"dependency cycle: {' -> '.join((*path, name))}")
        state[name] = 1
        for d in products[name].depends_on:
            visit(d, (*path, name))
        state[name] = 2

    for name in sorted(products):
        visit(name, ())


@dataclass(frozen=True)
class ImpactedConsumer:
    name: str
    usage: str
    dependency: str
    via: str  # product the consumer reads directly


@dataclass(frozen=True)
class ProductImpact:
    name: str
    version: str
    owner: Owner
    criticality: Criticality
    effective_criticality: Criticality
    freshness_hours: int
    min_score: float
    upstream_sources: tuple[str, ...]
    upstream_products: tuple[str, ...]
    downstream_products: tuple[str, ...]
    impacted: tuple[ImpactedConsumer, ...]


@dataclass(frozen=True)
class SourceImpact:
    name: str
    effective_criticality: Criticality
    products: tuple[str, ...]


@dataclass(frozen=True)
class ImpactMatrix:
    products: tuple[ProductImpact, ...]
    sources: tuple[SourceImpact, ...]

    def critical(self) -> list[ProductImpact]:
        return [p for p in self.products if p.effective_criticality is Criticality.CRITICAL]


def _closure(start: str, edges: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    todo = list(edges.get(start, ()))
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo.extend(edges.get(n, ()))
    return seen


def impact_matrix(products: Iterable[DataProduct]) -> ImpactMatrix:
    """Pure: products x consumers, effective criticality, upstream chain (deterministic order)."""
    by_name = {p.name: p for p in products}
    up = {n: {d for d in p.depends_on if d in by_name} for n, p in by_name.items()}
    down: dict[str, set[str]] = {n: set() for n in by_name}
    for n, deps in up.items():
        for d in deps:
            down[d].add(n)
    rows: list[ProductImpact] = []
    for n, p in by_name.items():
        ups, downs = _closure(n, up), _closure(n, down)
        eff = max([p.criticality, *(by_name[d].criticality for d in downs)], key=lambda c: c.rank)
        impacted = sorted(
            (ImpactedConsumer(c.name, c.usage, c.dependency, via)
             for via in (n, *downs) for c in by_name[via].consumers),
            key=lambda c: (c.via != n, c.via, c.name),
        )  # fmt: skip
        sources = {s for x in (n, *ups) for s in by_name[x].sources}
        rows.append(
            ProductImpact(
                n, p.version, p.owner, p.criticality, eff, p.sla.freshness_hours,
                p.quality.min_score, tuple(sorted(sources)), tuple(sorted(ups)),
                tuple(sorted(downs)), tuple(impacted),
            )
        )  # fmt: skip
    rows.sort(key=lambda r: (-r.effective_criticality.rank, -r.criticality.rank, r.name))
    src_names = sorted({s for r in rows for s in r.upstream_sources})
    srcs = []
    for s in src_names:
        users = [r for r in rows if s in r.upstream_sources]
        eff = max((r.effective_criticality for r in users), key=lambda c: c.rank)
        srcs.append(SourceImpact(s, eff, tuple(sorted(r.name for r in users))))
    srcs.sort(key=lambda s: (-s.effective_criticality.rank, s.name))
    return ImpactMatrix(tuple(rows), tuple(srcs))


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _crit(c: Criticality) -> str:
    return f"{_LABEL[c]} (`{c.value}`)"


def _dep(d: str) -> str:
    return "forte" if d == "hard" else "faible"


def render_bia_markdown(matrix: ImpactMatrix) -> str:
    """Deterministic French BIA document (docs/bia.md), critical products first."""
    out = [
        "# Analyse d'impact (BIA) des data products",
        "",
        "> Fichier généré par `uv run adp products bia` depuis `config/products/*.yaml` :"
        " ne pas éditer à la main.",
        "> Owners et consommateurs sont des équipes et rôles fictifs (H-05).",
        "",
        "Criticité effective d'un produit = max(criticité propre, criticité des produits aval)."
        " Une source amont hérite de la criticité effective maximale des produits construits"
        " dessus.",
        "",
        "## Produits critiques",
        "",
    ]
    crit = matrix.critical()
    if crit:
        out += [
            "| Produit | Criticité propre | Owner | SLA fraîcheur | Consommateurs impactés |",
            "|---|---|---|---|---|",
        ]
        out += [
            f"| `{r.name}` | {_crit(r.criticality)} | {_cell(r.owner.team)} "
            f"| {r.freshness_hours} h | {len(r.impacted)} |"
            for r in crit
        ]
    else:
        out.append("_Aucun produit critique._")
    out += [
        "",
        "## Matrice produits × consommateurs",
        "",
        "| Produit | Criticité effective | SLA fraîcheur | Consommateur | Usage"
        " | Dépendance | Via |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in matrix.products:
        for c in r.impacted:
            out.append(
                f"| `{r.name}` | {_crit(r.effective_criticality)} | {r.freshness_hours} h "
                f"| {_cell(c.name)} | {_cell(c.usage)} | {_dep(c.dependency)} | `{c.via}` |"
            )
    out += [
        "",
        "## Sources amont",
        "",
        "| Source | Criticité effective | Produits dépendants |",
        "|---|---|---|",
    ]
    out += [
        f"| `{s.name}` | {_crit(s.effective_criticality)} "
        f"| {', '.join(f'`{p}`' for p in s.products)} |"
        for s in matrix.sources
    ]
    out += ["", "## Détail par produit"]
    for r in matrix.products:
        chain = ", ".join(f"`{s}`" for s in r.upstream_sources) or "aucune"
        ups = ", ".join(f"`{p}`" for p in r.upstream_products) or "aucun"
        downs = ", ".join(f"`{p}`" for p in r.downstream_products) or "aucun"
        out += [
            "",
            f"### `{r.name}` (v{r.version})",
            "",
            f"- Owner : {_cell(r.owner.team)} ({_cell(r.owner.role)})",
            f"- Criticité : propre {_crit(r.criticality)}, effective"
            f" {_crit(r.effective_criticality)}",
            f"- SLA fraîcheur : {r.freshness_hours} h",
            f"- Score qualité minimal : {r.min_score:g}",
            f"- Chaîne amont : sources {chain} ; produits {ups}",
            f"- Produits aval : {downs}",
            "",
            "Si ce produit est indisponible, sont impactés :",
            "",
        ]
        out += [
            f"- {_cell(c.name)} : {_cell(c.usage)} (dépendance {_dep(c.dependency)}"
            + ("" if c.via == r.name else f", via `{c.via}`")
            + ")"
            for c in r.impacted
        ]
    return "\n".join(out) + "\n"
