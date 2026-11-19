"""Quality report per run (P4-05): stable JSON and a French HTML page for a business reader.

Pure builder (`build_report`, no clock: `generated_at` is injected) and renderer (`render_html`,
Jinja2 with autoescape). Reports hold column names, rule ids, counts, row INDICES, scores and
drift statistics only: never a data value, never the outlier bounds (they are derived from the
batch and can reveal its order of magnitude).

`schema_version` is bumped on any breaking change of the JSON layout.
"""

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from adp.errors import QualityError
from adp.quality.anomaly import AnomalyResult
from adp.quality.drift import DriftResult
from adp.quality.quarantine import Decision
from adp.quality.rules import QualityReport
from adp.quality.score import BatchScore

SCHEMA_VERSION = 1
MAX_LISTED_ROWS = 10
MAX_TOP_N = 50
TEMPLATES = Path(__file__).parent / "templates"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(default=True, default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _check_run_id(run_id: str) -> None:
    """Refuse anything that could leave the report directory (no separator, no leading dot)."""
    if not _RUN_ID.fullmatch(run_id):
        raise QualityError("run_id invalide (lettres, chiffres, '.', '_', '-' ; 64 max)")


def _anomaly_entries(anomalies: Mapping[str, AnomalyResult], top_n: int) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = [
        {
            "column": column,
            "method": a.method,
            "evaluated": a.evaluated,
            "outlier_count": a.outlier_count,
            "outlier_rate": round(a.outlier_count / a.evaluated, 6) if a.evaluated else 0.0,
            "row_indices": list(a.outlier_indices[:MAX_LISTED_ROWS]),
        }
        for column, a in anomalies.items()
        if not a.skipped and a.outlier_count > 0
    ]
    found.sort(key=lambda e: (-e["outlier_count"], e["column"]))
    return found[:top_n]


def _drift_entries(drift: Mapping[str, DriftResult]) -> list[dict[str, Any]]:
    return [
        {
            "column": column,
            "kind": d.kind,
            "sizes": list(d.sizes),
            "psi": d.psi,
            "psi_level": d.psi_level,
            "ks_pvalue": d.ks_pvalue,
            "drifted": d.drifted,
            "reason": d.reason,
            "skipped_reason": d.skipped_reason,
        }
        for column, d in drift.items()
    ]


def build_report(
    *,
    run_id: str,
    source: str,
    generated_at: datetime,
    quality: QualityReport,
    score: BatchScore,
    anomalies: Mapping[str, AnomalyResult] | None = None,
    drift: Mapping[str, DriftResult] | None = None,
    decision: Decision | None = None,
    top_n: int = 5,
) -> dict[str, Any]:
    """Build the JSON-serialisable report of one run. Raises QualityError on an invalid run_id."""
    _check_run_id(run_id)
    if not 1 <= top_n <= MAX_TOP_N:
        raise QualityError(f"top_n hors bornes (1 à {MAX_TOP_N})")
    failed_rules = [
        {
            "rule_id": r.rule_id,
            "type": r.type,
            "severity": r.severity,
            "level": r.level,
            "column": r.detail.get("column"),
            "evaluated": r.evaluated,
            "failed_count": r.failed_count,
            "failed_rows": r.failed_rows[:MAX_LISTED_ROWS],
            "message": r.message,
        }
        for r in quality.results
        if r.status == "failed"
    ]
    quarantine = None
    if decision is not None:
        quarantine = {
            "level": decision.level,
            "alert": decision.alert,
            "failed_rate": decision.failed_rate,
            "batch_reasons": list(decision.batch_reasons),
            "quarantined_rows": len(decision.quarantine_rows),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "source": source,
        "generated_at": generated_at.isoformat(),
        "summary": {
            "status": score.status,
            "score": score.score,
            "rows_total": quality.rows_total,
            "rows_failed": score.rows_failed,
            "ok": quality.ok,
            "dimensions": {
                "completeness": score.completeness,
                "validity": score.validity,
                "uniqueness": score.uniqueness,
                "freshness": score.freshness,
            },
        },
        "columns": [c.model_dump() for c in score.columns],
        "failed_rules": failed_rules,
        "top_anomalies": _anomaly_entries(anomalies or {}, top_n),
        "drift": _drift_entries(drift or {}),
        "quarantine": quarantine,
    }


def render_html(report: Mapping[str, Any]) -> str:
    """Render the report as one self-contained French HTML page (inline CSS, no script)."""
    template = _env.get_template("quality_report.html.j2")
    return template.render(report=report, sample_rows=MAX_LISTED_ROWS)


def write_report(report: Mapping[str, Any], directory: Path) -> tuple[Path, Path]:
    """Write `<run_id>.json` and `<run_id>.html` in `directory`; returns both paths."""
    run_id = str(report["run_id"])
    _check_run_id(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{run_id}.json"
    html_path = directory / f"{run_id}.html"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path
