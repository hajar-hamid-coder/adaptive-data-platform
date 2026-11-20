"""Data product contracts and BIA impact matrix (P5-01)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from adp.cli import main
from adp.errors import ConfigError, ContractError
from adp.gold.contract import (
    Criticality,
    DataProduct,
    impact_matrix,
    load_product,
    load_products,
    render_bia_markdown,
)

ROOT = Path(__file__).parents[2]
PRODUCTS = ROOT / "config" / "products"
BIA = ROOT / "docs" / "bia.md"


def _base(**over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "name": "pop",
        "version": "1.0.0",
        "description": "Population.",
        "sources": ["hcp_population"],
        "columns": [{"name": "region", "type": "string"}, {"name": "year", "type": "integer"}],
        "owner": {"team": "Équipe Démographie", "role": "Product owner"},
        "sla": {"freshness_hours": 24},
        "criticality": "high",
        "consumers": [{"name": "Tableau de bord", "usage": "Pilotage", "dependency": "hard"}],
        "quality": {"min_score": 85},
    }
    raw.update(over)
    return {k: v for k, v in raw.items() if v is not None}


def _write(d: Path, raw: dict[str, Any]) -> Path:
    p = d / f"{raw['name']}.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return p


def test_valid_contract_loads(tmp_path: Path) -> None:
    p = load_product(_write(tmp_path, _base()))
    assert p.owner.team == "Équipe Démographie"
    assert p.sla.freshness_hours == 24
    assert p.criticality is Criticality.HIGH


@pytest.mark.parametrize("missing", ["owner", "sla"])
def test_contract_without_owner_or_sla_is_refused(tmp_path: Path, missing: str) -> None:
    with pytest.raises(ContractError, match=missing):
        load_product(_write(tmp_path, _base(**{missing: None})))


@pytest.mark.parametrize(
    "over",
    [
        {"owner": {"team": "  ", "role": "PO"}},
        {"sla": {"freshness_hours": 0}},
        {"criticality": "urgent"},
        {"columns": [{"name": "salary", "type": "float"}]},  # not canonical
        {"columns": [{"name": "year", "type": "string"}]},  # type mismatch
        {"columns": [{"name": "year", "type": "integer"}] * 2},  # duplicate
        {"quality": {"min_score": 50}},  # below promotion gate (80)
        {"sources": ["unknown_source"]},
        {"name": "Bad-Name"},
        {"owner": {"team": "T", "role": "PO", "email": "x@example.com"}},  # extra forbidden
    ],
)
def test_invalid_contracts_are_refused(tmp_path: Path, over: dict[str, Any]) -> None:
    raw = _base(**over)
    path = tmp_path / "pop.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ContractError):
        load_product(path)


def test_error_message_carries_no_input_value(tmp_path: Path) -> None:
    secret = "s3cr3t-value"
    raw = _base(owner={"team": "T", "role": "PO", "phone": secret})
    with pytest.raises(ContractError) as exc:
        load_product(_write(tmp_path, raw))
    assert secret not in str(exc.value)
    assert "owner.phone" in str(exc.value)


def test_contract_error_is_a_config_error() -> None:
    assert issubclass(ContractError, ConfigError)


def test_file_name_must_match_product_name(tmp_path: Path) -> None:
    path = tmp_path / "other.yaml"
    path.write_text(yaml.safe_dump(_base()), encoding="utf-8")
    with pytest.raises(ContractError, match="file name"):
        load_product(path)


def test_load_products_rejects_duplicates_unknown_deps_and_cycles(tmp_path: Path) -> None:
    _write(tmp_path, _base(name="a", depends_on=["b"]))
    _write(tmp_path, _base(name="b", depends_on=["a"]))
    with pytest.raises(ContractError, match="cycle"):
        load_products(tmp_path)
    _write(tmp_path, _base(name="b", depends_on=["ghost"]))
    with pytest.raises(ContractError, match="ghost"):
        load_products(tmp_path)
    dup = tmp_path / "sub"
    dup.mkdir()
    (dup / "a.yaml").write_text(yaml.safe_dump(_base(name="a")), encoding="utf-8")
    (dup / "a.yml").write_text(yaml.safe_dump(_base(name="a")), encoding="utf-8")
    with pytest.raises(ContractError, match="duplicate"):
        load_products(dup)


def test_repo_products_are_valid_and_cover_criticalities() -> None:
    products = load_products(PRODUCTS)
    assert len(products) >= 3
    levels = {p.criticality for p in products}
    assert {Criticality.CRITICAL, Criticality.LOW} <= levels


def _p(
    name: str, crit: str, deps: tuple[str, ...] = (), src: str = "hcp_population"
) -> DataProduct:
    return DataProduct.model_validate(
        _base(name=name, criticality=crit, depends_on=list(deps), sources=[src])
    )


def test_impact_matrix_lists_critical_first_and_propagates_upstream() -> None:
    products = [
        _p("raw", "low", src="world_bank_gdp"),
        _p("summary", "critical", deps=("raw",)),
        _p("other", "medium"),
    ]
    m = impact_matrix(products)
    assert [r.name for r in m.products] == ["summary", "raw", "other"]
    raw = next(r for r in m.products if r.name == "raw")
    assert raw.criticality is Criticality.LOW
    assert raw.effective_criticality is Criticality.CRITICAL
    assert raw.downstream_products == ("summary",)
    summary = m.products[0]
    assert summary.upstream_sources == ("hcp_population", "world_bank_gdp")
    assert {s.name: s.effective_criticality for s in m.sources} == {
        "hcp_population": Criticality.CRITICAL,
        "world_bank_gdp": Criticality.CRITICAL,
    }
    assert [r.name for r in m.critical()] == ["summary", "raw"]


def test_bia_markdown_lists_critical_products_and_impacts() -> None:
    md = render_bia_markdown(impact_matrix(load_products(PRODUCTS)))
    crit = [p.name for p in load_products(PRODUCTS) if p.criticality is Criticality.CRITICAL]
    section = md.split("## Produits critiques", 1)[1].split("\n## ", 1)[0]
    for name in crit:
        assert name in section
    assert "Si ce produit est indisponible" in md
    assert md == render_bia_markdown(impact_matrix(list(reversed(load_products(PRODUCTS)))))


def test_docs_bia_is_in_sync_with_contracts() -> None:
    expected = render_bia_markdown(impact_matrix(load_products(PRODUCTS)))
    assert BIA.read_text(encoding="utf-8") == expected, "run: uv run adp products bia"


def test_cli_validate_and_bia(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["products", "validate", "--dir", str(PRODUCTS)]) == 0
    out = tmp_path / "bia.md"
    assert main(["products", "bia", "--dir", str(PRODUCTS), "--output", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == BIA.read_text(encoding="utf-8")
    _write(tmp_path, _base(owner=None))
    assert main(["products", "validate", "--dir", str(tmp_path)]) == 2
    assert "owner" in capsys.readouterr().err
