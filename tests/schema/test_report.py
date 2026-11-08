"""Évolution : rapport Markdown/JSON, événement SchemaChanged unique et idempotence (P2-05).

Données synthétiques du simulateur (tests/data/drift). Registre sur SQLite, comme test_registry.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine

from adp.connectors.files import read_file
from adp.errors import SchemaError
from adp.schema.diff import ChangeKind
from adp.schema.events import EventBus, SchemaChanged
from adp.schema.policy import ChangeImpact, EvolutionAction, EvolutionPolicy, load_policy
from adp.schema.profile import SchemaFingerprint, profile_table
from adp.schema.registry import SchemaRegistry, metadata
from adp.schema.report import EvolutionReport, render_json, render_markdown, report_evolution

ROOT = Path(__file__).parent.parent.parent
POLICY_FILE = ROOT / "config" / "evolution_policy.yaml"
DRIFT = ROOT / "tests" / "data" / "drift"
SRC = "hcp_population"
PK = ("region", "annee")
SAMPLE_VALUES = (
    "Tanger-Tétouan-Al Hoceïma",
    "1080551",
)  # présentes dans v1 : ne doivent pas fuiter


def drift(v: str) -> SchemaFingerprint:
    return profile_table(read_file(DRIFT / v / "population.csv"))


@pytest.fixture(scope="module")
def policy() -> EvolutionPolicy:
    return load_policy(POLICY_FILE)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def reg(engine: Engine) -> SchemaRegistry:
    return SchemaRegistry(engine)


def make_bus() -> tuple[EventBus, list[SchemaChanged]]:
    bus = EventBus()
    received: list[SchemaChanged] = []
    bus.subscribe(received.append)
    return bus, received


def run(
    reg: SchemaRegistry,
    version: str,
    bus: EventBus,
    policy: EvolutionPolicy,
    previous: SchemaFingerprint | None = None,
) -> EvolutionReport | None:
    return report_evolution(
        reg, SRC, drift(version), policy=policy, primary_key=PK, bus=bus, previous=previous
    )


# --- backlog validation test -----------------------------------------------------------------


def test_report_v1_to_v2_is_compatible(reg: SchemaRegistry, policy: EvolutionPolicy) -> None:
    bus, _ = make_bus()
    run(reg, "v1", bus, policy)
    report = run(reg, "v2", bus, policy, previous=drift("v1"))

    assert report is not None
    assert (report.from_version, report.to_version) == (1, 2)
    assert (report.from_semver, report.to_semver) == ("1.0.0", "2.0.0")
    assert report.overall_impact is ChangeImpact.COMPATIBLE
    assert report.overall_action is EvolutionAction.AUTO_APPLY

    renamed = [c for c in report.changes if c.kind is ChangeKind.RENAMED]
    assert [(c.old_column, c.column) for c in renamed] == [("population", "population_totale")]
    assert renamed[0].rename is not None and renamed[0].rename.decision == "auto"
    assert [c.column for c in report.changes if c.kind is ChangeKind.ADDED] == ["taux_activite"]

    md = render_markdown(report)
    assert "v1" in md and "v2" in md and "1.0.0" in md and "2.0.0" in md
    assert "population → population_totale" in md and "taux_activite" in md
    assert "Compatible" in md


def test_report_v2_to_v3_needs_review(reg: SchemaRegistry, policy: EvolutionPolicy) -> None:
    bus, _ = make_bus()
    run(reg, "v1", bus, policy)
    run(reg, "v2", bus, policy, previous=drift("v1"))
    report = run(reg, "v3", bus, policy, previous=drift("v2"))

    assert report is not None
    assert (report.from_version, report.to_version) == (2, 3)
    assert (report.from_semver, report.to_semver) == ("2.0.0", "3.0.0")
    assert report.overall_impact is ChangeImpact.REVIEW
    assert report.overall_action is EvolutionAction.QUEUE_REVIEW
    removed = [c for c in report.changes if c.kind is ChangeKind.REMOVED]
    assert [c.column for c in removed] == ["taux_activite"]
    assert removed[0].impact is ChangeImpact.REVIEW
    assert "taux_activite" in render_markdown(report)


def test_explicit_baseline_is_the_diff_reference(
    reg: SchemaRegistry, policy: EvolutionPolicy
) -> None:
    """`baseline` replaces `latest` as reference; the change log stores (baseline -> new)."""
    bus, events = make_bus()
    run(reg, "v1", bus, policy)
    run(reg, "v3", bus, policy, previous=drift("v1"))  # v2 in the registry, not the baseline
    v1 = reg.get(SRC, 1)
    report = report_evolution(
        reg, SRC, drift("v2"), policy=policy, primary_key=PK, bus=bus,
        previous=drift("v1"), baseline=v1,
    )  # fmt: skip
    assert report is not None and (report.from_version, report.to_version) == (1, 3)
    assert {r.from_version for r in reg.changes(SRC, 3)} == {1}
    assert events[-1].from_version == 1
    with pytest.raises(SchemaError, match="does not match v1"):
        report_evolution(
            reg, SRC, drift("v2").model_copy(update={"fingerprint": "x" * 64}), policy=policy,
            primary_key=PK, bus=bus, previous=drift("v3"), baseline=v1,
        )  # fmt: skip


# --- événement -------------------------------------------------------------------------------


def test_event_emitted_once_per_change(reg: SchemaRegistry, policy: EvolutionPolicy) -> None:
    bus, events = make_bus()
    run(reg, "v1", bus, policy)
    v2 = run(reg, "v2", bus, policy, previous=drift("v1"))
    assert len(events) == 2

    # rappel avec le même schéma, et retour à v1 déjà connue : rien de nouveau
    assert run(reg, "v2", bus, policy, previous=drift("v1")) is None
    assert run(reg, "v1", bus, policy, previous=drift("v2")) is None
    assert len(events) == 2 and len(reg.history(SRC)) == 2

    assert v2 is not None
    last = events[-1]
    assert (last.source_name, last.from_version, last.to_version) == (SRC, 1, 2)
    assert (last.semver, last.change_count) == ("2.0.0", len(v2.changes))
    assert last.overall_impact is ChangeImpact.COMPATIBLE


def test_first_version_report_and_single_event(
    reg: SchemaRegistry, policy: EvolutionPolicy
) -> None:
    bus, events = make_bus()
    report = run(reg, "v1", bus, policy)

    assert report is not None
    assert report.from_version is None and report.from_semver is None
    assert (report.to_version, report.to_semver) == (1, "1.0.0")
    assert report.changes == ()
    assert report.overall_action is EvolutionAction.AUTO_APPLY
    assert "Version initiale" in render_markdown(report)
    assert len(events) == 1 and events[0].from_version is None and events[0].change_count == 0
    assert reg.changes(SRC, 1) == []


def test_changed_schema_requires_matching_previous(
    reg: SchemaRegistry, policy: EvolutionPolicy
) -> None:
    bus, events = make_bus()
    run(reg, "v1", bus, policy)
    with pytest.raises(SchemaError, match="previous profile required"):
        run(reg, "v2", bus, policy)
    with pytest.raises(SchemaError, match="does not match"):
        run(reg, "v2", bus, policy, previous=drift("v3"))
    # rien n'a été écrit ni publié par les appels refusés
    assert reg.latest(SRC) is not None and reg.latest(SRC).version == 1  # type: ignore[union-attr]
    assert len(events) == 1


# --- bus -------------------------------------------------------------------------------------


def test_failing_subscriber_does_not_block_others(
    reg: SchemaRegistry, policy: EvolutionPolicy
) -> None:
    def broken(_: SchemaChanged) -> None:
        raise RuntimeError("boom")

    bus = EventBus()
    bus.subscribe(broken)
    received: list[SchemaChanged] = []
    bus.subscribe(received.append)

    report = run(reg, "v1", bus, policy)
    assert report is not None  # l'émetteur n'échoue pas
    assert len(received) == 1


# --- rendu : aucune valeur de donnée, JSON déterministe --------------------------------------


def test_reports_contain_no_data_value(reg: SchemaRegistry, policy: EvolutionPolicy) -> None:
    bus, _ = make_bus()
    run(reg, "v1", bus, policy)
    report = run(reg, "v2", bus, policy, previous=drift("v1"))
    assert report is not None
    for text in (render_markdown(report), render_json(report)):
        for value in SAMPLE_VALUES:
            assert value not in text


def test_json_deterministic_and_reloadable(reg: SchemaRegistry, policy: EvolutionPolicy) -> None:
    bus, _ = make_bus()
    run(reg, "v1", bus, policy)
    report = run(reg, "v2", bus, policy, previous=drift("v1"))
    assert report is not None

    text = render_json(report)
    assert text == render_json(report)
    assert text == json.dumps(json.loads(text), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    assert EvolutionReport.model_validate_json(text) == report
