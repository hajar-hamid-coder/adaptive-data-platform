"""Structural checks on architecture docs and ADRs (P0-07)."""

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs"
ADR_DIR = DOCS / "adr"
ARCHITECTURE = DOCS / "architecture.md"
ADR_PATTERN = re.compile(r"^\d{4}-[a-z0-9-]+\.md$")
SECTIONS = ("## Contexte", "## Décision", "## Conséquences")


def _adrs() -> list[Path]:
    return sorted(p for p in ADR_DIR.glob("*.md") if ADR_PATTERN.match(p.name))


def test_at_least_six_numbered_adrs() -> None:
    names = [p.name for p in _adrs()]
    assert len(names) >= 6, names
    assert "0001-medallion.md" in names
    assert "0006-llm-policy.md" in names


@pytest.mark.parametrize("adr", _adrs(), ids=lambda p: p.name)
def test_adr_has_required_sections(adr: Path) -> None:
    lines = {line.strip() for line in adr.read_text(encoding="utf-8").splitlines()}
    missing = [s for s in SECTIONS if s not in lines]
    assert not missing, f"{adr.name} missing {missing}"
    assert adr.read_text(encoding="utf-8").startswith("# "), f"{adr.name} lacks a title"


def test_architecture_has_mermaid_flowchart() -> None:
    text = ARCHITECTURE.read_text(encoding="utf-8")
    blocks = re.findall(r"^```mermaid\n(.*?)^```", text, flags=re.M | re.S)
    assert blocks, "no ```mermaid block"
    first = blocks[0].lstrip().splitlines()[0]
    assert re.match(r"^(flowchart|graph)\s+(TD|TB|BT|LR|RL)\b", first), first
    lines = [line.strip() for line in blocks[0].splitlines()]
    assert any("-->" in line for line in lines)
    opened = sum(line.startswith("subgraph ") for line in lines)
    assert opened == lines.count("end"), "unbalanced subgraph/end"


def test_architecture_links_to_existing_adrs() -> None:
    text = ARCHITECTURE.read_text(encoding="utf-8")
    links = re.findall(r"\]\((adr/[^)#]+\.md)\)", text)
    assert len(set(links)) >= 6, links
    for link in links:
        assert (DOCS / link).is_file(), f"broken link {link}"
