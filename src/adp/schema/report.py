"""Rapport d'évolution de schéma en Markdown et JSON, et orchestration du changement (P2-05).

Périmètre : noms de colonnes, types, versions, semver, impacts et actions uniquement. Aucune
valeur de donnée n'entre dans le rapport (ni les exemples du profil, ni les formats bruts).

Fonctions pures : `build_report`, `render_markdown`, `render_json`. Seule `report_evolution`
écrit (registre) et publie (bus).

Décisions de `report_evolution` :
- Schéma identique à une version déjà connue (même fingerprint, y compris un retour à une
  version antérieure) : aucune écriture, aucun changement, aucun événement, retour `None`.
  Rejouer deux fois le même schéma ne ré-émet donc rien.
- Première version d'une source : rapport « version initiale » sans diff (pas de comparaison à
  faire), aucun changement enregistré, et un événement `SchemaChanged` publié une fois
  (`change_count = 0`), pour que les abonnés voient l'apparition de la source.
- Changement d'une source déjà connue : le diff a besoin du profil PRÉCÉDENT (les statistiques
  alimentent le score des renommages, que le registre ne stocke pas). Le profil `previous` est
  donc exigé et vérifié contre la dernière version du registre avant toute écriture ; sinon
  `SchemaError`, sans effet de bord.
- Référence du diff (`baseline`, correctif P2-06) : par défaut `"latest"` (dernière version du
  registre, comportement historique). Le schema gate passe explicitement la dernière version
  ACCEPTÉE (`SchemaRegistry.baseline`), ou `None` si aucune ne l'est (rapport « version
  initiale »). Le journal `schema_change` enregistre la paire (référence → nouvelle version).
"""

import json
from collections.abc import Collection
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from adp.errors import SchemaError
from adp.logging import get_logger
from adp.schema.diff import ChangeKind, diff_schemas
from adp.schema.events import EventBus, SchemaChanged
from adp.schema.policy import (
    ChangeImpact,
    ClassifiedChange,
    DiffImpact,
    EvolutionAction,
    EvolutionPolicy,
    classify_diff,
    with_impact,
)
from adp.schema.profile import SchemaFingerprint
from adp.schema.registry import SchemaRegistry, SchemaVersion

log = get_logger(__name__)

KIND_LABEL: Final[dict[ChangeKind, str]] = {
    ChangeKind.ADDED: "Colonne ajoutée",
    ChangeKind.REMOVED: "Colonne supprimée",
    ChangeKind.RENAMED: "Colonne renommée",
    ChangeKind.TYPE_CHANGED: "Type modifié",
    ChangeKind.FORMAT_CHANGED: "Format modifié",
    ChangeKind.NULLABILITY_CHANGED: "Nullabilité modifiée",
    ChangeKind.REORDERED: "Ordre modifié",
}
IMPACT_LABEL: Final[dict[ChangeImpact, str]] = {
    ChangeImpact.COMPATIBLE: "Compatible",
    ChangeImpact.REVIEW: "À revoir",
    ChangeImpact.BLOCKING: "Bloquant",
}
ACTION_LABEL: Final[dict[EvolutionAction, str]] = {
    EvolutionAction.AUTO_APPLY: "Application automatique",
    EvolutionAction.QUEUE_REVIEW: "File de validation",
    EvolutionAction.BLOCK: "Blocage",
}
SECTION_TITLE: Final[dict[ChangeImpact, str]] = {
    ChangeImpact.BLOCKING: "Changements bloquants",
    ChangeImpact.REVIEW: "Changements à revoir",
    ChangeImpact.COMPATIBLE: "Changements compatibles",
}
_SECTION_ORDER: Final = (ChangeImpact.BLOCKING, ChangeImpact.REVIEW, ChangeImpact.COMPATIBLE)


class RenameScore(BaseModel):
    """Score d'un renommage et ses composantes (aucune valeur de donnée)."""

    model_config = ConfigDict(frozen=True)

    score: float
    name_sim: float
    type_sim: float
    dist_sim: float
    decision: str


class ReportChange(BaseModel):
    """Un changement du rapport, avec son impact, son action et la règle qui l'a décidé."""

    model_config = ConfigDict(frozen=True)

    kind: ChangeKind
    column: str | None
    old_column: str | None
    impact: ChangeImpact
    action: EvolutionAction
    rule_id: str
    reason: str
    rename: RenameScore | None = None


class EvolutionReport(BaseModel):
    """Rapport d'un changement de schéma. `from_version` est None pour une version initiale."""

    model_config = ConfigDict(frozen=True)

    source_name: str
    from_version: int | None
    from_semver: str | None
    to_version: int
    to_semver: str
    created_at: datetime
    overall_impact: ChangeImpact
    overall_action: EvolutionAction
    changes: tuple[ReportChange, ...]


def _report_change(c: ClassifiedChange) -> ReportChange:
    rename = None
    if c.change.kind is ChangeKind.RENAMED:
        d = c.change.detail
        comp = d["components"]
        rename = RenameScore(
            score=d["score"],
            name_sim=comp["name_sim"],
            type_sim=comp["type_sim"],
            dist_sim=comp["dist_sim"],
            decision=d["decision"],
        )
    return ReportChange(
        kind=c.change.kind,
        column=c.change.column,
        old_column=c.change.old_column,
        impact=c.impact,
        action=c.action,
        rule_id=c.rule_id,
        reason=c.reason,
        rename=rename,
    )


def build_report(
    old: SchemaVersion | None, new: SchemaVersion, impact: DiffImpact
) -> EvolutionReport:
    """Assemble the report from the versions and the classified diff (pure)."""
    return EvolutionReport(
        source_name=new.source_name,
        from_version=old.version if old else None,
        from_semver=old.semver if old else None,
        to_version=new.version,
        to_semver=new.semver,
        created_at=new.created_at,
        overall_impact=impact.overall_impact,
        overall_action=impact.overall_action,
        changes=tuple(_report_change(c) for c in impact.classified),
    )


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _kind_text(c: ReportChange) -> str:
    text = KIND_LABEL[c.kind]
    if c.rename is not None:
        r = c.rename
        text += (
            f" (score {r.score:.2f} : nom {r.name_sim:.2f}, type {r.type_sim:.2f}, "
            f"distribution {r.dist_sim:.2f}, décision {r.decision})"
        )
    return text


def _column_text(c: ReportChange) -> str:
    if c.column is None:
        return "—"
    if c.old_column and c.old_column != c.column:
        return f"{c.old_column} → {c.column}"
    return c.column


def render_markdown(report: EvolutionReport) -> str:
    """Rapport lisible en français : en-tête, résumé, une section par impact (aucune valeur)."""
    head = (
        f"**Version initiale** : v{report.to_version} ({report.to_semver})"
        if report.from_version is None
        else f"**Version** : v{report.from_version} ({report.from_semver}) → "
        f"v{report.to_version} ({report.to_semver})"
    )
    lines = [
        f"# Rapport d'évolution du schéma : {_cell(report.source_name)}",
        "",
        head,
        "",
        f"**Date** : {report.created_at.isoformat(timespec='seconds')}",
        "",
        f"**Impact global** : {IMPACT_LABEL[report.overall_impact]} — "
        f"**Action globale** : {ACTION_LABEL[report.overall_action]}",
        "",
        "## Résumé",
        "",
        "| Impact | Changements |",
        "|---|---:|",
    ]
    for impact in _SECTION_ORDER:
        count = sum(1 for c in report.changes if c.impact is impact)
        lines.append(f"| {IMPACT_LABEL[impact]} | {count} |")
    for impact in _SECTION_ORDER:
        lines += ["", f"## {SECTION_TITLE[impact]}", ""]
        group = [c for c in report.changes if c.impact is impact]
        if not group:
            lines.append("Aucun changement.")
            continue
        lines += [
            "| Changement | Colonne | Action | Règle | Raison |",
            "|---|---|---|---|---|",
        ]
        for c in group:
            lines.append(
                f"| {_cell(_kind_text(c))} | {_cell(_column_text(c))} | "
                f"{ACTION_LABEL[c.action]} | `{c.rule_id}` | {_cell(c.reason)} |"
            )
    return "\n".join(lines) + "\n"


def render_json(report: EvolutionReport) -> str:
    """JSON déterministe : clés triées, indentation fixe, pas d'horodatage ajouté."""
    data = report.model_dump(mode="json")
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def report_evolution(
    registry: SchemaRegistry,
    source_name: str,
    new_fingerprint: SchemaFingerprint,
    *,
    policy: EvolutionPolicy,
    primary_key: Collection[str],
    bus: EventBus,
    previous: SchemaFingerprint | None = None,
    baseline: SchemaVersion | None | Literal["latest"] = "latest",
    run_id: str | None = None,
) -> EvolutionReport | None:
    """Register the new schema, diff it with `baseline` (default: the latest version), record
    the (baseline -> new) changes and publish once.

    Returns None (no write, no event) when the schema is already known. See the module
    docstring for the first-version and `previous` decisions. Raises SchemaError on a
    missing or mismatching `previous` before anything is written.
    """
    known = {v.fingerprint for v in registry.history(source_name)}
    if new_fingerprint.fingerprint in known:
        log.info("schema_unchanged", source_name=source_name)
        return None

    latest = registry.latest(source_name) if baseline == "latest" else baseline
    if latest is None:
        impact = DiffImpact(
            classified=(),
            overall_impact=ChangeImpact.COMPATIBLE,
            overall_action=EvolutionAction.AUTO_APPLY,
        )
        new = registry.register(source_name, new_fingerprint)
    else:
        if previous is None:
            raise SchemaError(
                f"{source_name}: previous profile required to diff against v{latest.version}"
            )
        if previous.fingerprint != latest.fingerprint:
            raise SchemaError(f"{source_name}: previous profile does not match v{latest.version}")
        new = registry.register(source_name, new_fingerprint)
        diff = diff_schemas(previous, new_fingerprint)
        impact = classify_diff(diff, policy=policy, primary_key=primary_key)
        registry.record_changes(source_name, latest.version, new.version, with_impact(impact))

    report = build_report(latest, new, impact)
    event = SchemaChanged(
        source_name=source_name,
        from_version=report.from_version,
        to_version=new.version,
        semver=new.semver,
        overall_impact=impact.overall_impact,
        overall_action=impact.overall_action,
        change_count=len(impact.classified),
        run_id=run_id,
    )
    bus.publish(event)
    log.info(
        "schema_changed",
        source_name=source_name,
        from_version=report.from_version,
        to_version=new.version,
        semver=new.semver,
        overall_impact=impact.overall_impact.value,
        change_count=event.change_count,
        run_id=run_id,
    )
    return report
