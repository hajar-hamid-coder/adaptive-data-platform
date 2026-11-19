"""Final Silver -> Gold gate (P4-07): a batch is promoted only if score and critical rules pass.

`evaluate_promotion` is pure. `promote_batch` evaluates the Silver batch, always writes the
quality report (JSON + HTML), then either marks the batch `gold_ready` or quarantines it.

Decisions:
- There is no real Gold before P5: "promotion" = idempotent copy of the Silver parquet plus a
  manifest under `gold_ready/<source>/<batch>/` (data first, manifest last, `put_if_absent`).
- Promoted only if: no failed `error` rule (row or batch level), no batch-level quarantine
  decision, score not n/a and >= `min_score` (config/quality/promotion.yaml, provisional).
  A failed `warn` rule never blocks.
- Quarantine is not an outage: the asset stays green with status `quarantined` in metadata.
- Reasons carry rule ids, counts and thresholds, never data values.
"""

import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.engine import Engine

from adp.bronze.store import ObjectStore
from adp.errors import ConfigError
from adp.logging import get_logger
from adp.mapping.dictionary import SemanticDictionary
from adp.mapping.transform import Value
from adp.quality.quarantine import (
    Decision,
    QuarantineStore,
    decide,
    load_quarantine_config,
)
from adp.quality.report import build_report, write_report
from adp.quality.rules import QualityReport, evaluate, load_references, load_rules
from adp.quality.score import BatchScore, QualityScoreStore, load_score_config, score_batch

log = get_logger(__name__)
QUALITY = Path("config/quality")
GOLD_PREFIX = "gold_ready"


class PromotionConfig(BaseModel):
    """Promotion threshold (provisional)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_score: float = Field(default=80.0, ge=0, le=100, allow_inf_nan=False)


def load_promotion_config(path: Path) -> PromotionConfig:
    """Load config/quality/promotion.yaml; errors name the file (no data value)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot read YAML ({type(exc).__name__})") from exc
    if not isinstance(raw, dict | None):
        raise ConfigError(f"{path}: expected a YAML mapping")
    try:
        return PromotionConfig.model_validate(raw or {})
    except ValidationError as exc:
        raise ConfigError(f"{path}: invalid promotion config") from exc


@dataclass(frozen=True)
class PromotionDecision:
    status: Literal["promoted", "quarantined"]
    reasons: list[str]


def evaluate_promotion(
    report: QualityReport, score: BatchScore, decision: Decision, config: PromotionConfig
) -> PromotionDecision:
    """Pure gate: reasons are rule ids and thresholds only."""
    reasons = [
        f"rule:{r.rule_id}"
        for r in report.results
        if r.severity == "error" and r.status == "failed"
    ]
    if decision.level == "batch":
        reasons += [f"quarantine:{x}" for x in decision.batch_reasons]
    if score.score is None:
        reasons.append("score:n/a")
    elif score.score < config.min_score:
        reasons.append(f"score={score.score}<{config.min_score}")
    return PromotionDecision("quarantined" if reasons else "promoted", reasons)


def read_silver_rows(store: ObjectStore, parquet_path: str) -> tuple[list[str], list[list[Value]]]:
    """Read a Silver parquet back as (columns, typed rows)."""
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "s.parquet"
        local.write_bytes(store.get(parquet_path))
        con = duckdb.connect()
        try:
            cur = con.execute("SELECT * FROM read_parquet(?)", [str(local)])
            cols = [d[0] for d in cur.description]
            return cols, [list(r) for r in cur.fetchall()]
        finally:
            con.close()


@dataclass(frozen=True)
class PromotionResult:
    status: Literal["promoted", "quarantined"]
    reasons: list[str]
    batch_id: str
    score: float | None
    rows: int
    rows_quarantined: int
    gold_path: str
    report_json: str
    report_html: str
    skipped: bool


def _promote(
    store: ObjectStore, silver: ObjectStore, source: str, batch: str, silver_path: str,
    rows: int, score: float | None, run_id: str,
) -> tuple[str, bool]:  # fmt: skip
    base = f"{GOLD_PREFIX}/{source}/{batch}"
    data_key, mf_key = f"{base}/data.parquet", f"{base}/manifest.json"
    if store.exists(mf_key):
        return data_key, True
    store.put_if_absent(data_key, silver.get(silver_path))
    manifest = {"source": source, "batch_id": batch, "silver_path": silver_path, "rows": rows,
                "score": score, "run_id": run_id, "data": data_key,
                "promoted_at": datetime.now(UTC).isoformat()}  # fmt: skip
    created = store.put_if_absent(mf_key, json.dumps(manifest, indent=2).encode())
    return data_key, not created


def promote_batch(
    *,
    source: str,
    silver_path: str,
    run_id: str,
    dictionary: SemanticDictionary,
    silver_store: ObjectStore,
    gold_store: ObjectStore,
    quarantine_store: ObjectStore,
    report_dir: Path,
    engine: Engine | None = None,
    schema_version: int | None = None,
    quality_dir: Path = QUALITY,
) -> PromotionResult:
    """Evaluate, report, then promote or quarantine one Silver batch. Raises QualityError."""
    rules = [
        *load_rules(quality_dir / "population.yaml", dictionary),
        *load_rules(quality_dir / "business_rules.yaml", dictionary),
    ]
    refs = load_references(rules, quality_dir / "reference")
    qcfg = load_quarantine_config(quality_dir / "quarantine.yaml")
    columns, rows = read_silver_rows(silver_store, silver_path)
    report = evaluate(rules, columns, rows, references=refs)
    scfg = load_score_config(quality_dir / "score.yaml", dictionary)
    score = score_batch(columns, rows, report, rules, config=scfg)
    decision = decide(report, qcfg, score)
    verdict = evaluate_promotion(report, score, decision,
                                 load_promotion_config(quality_dir / "promotion.yaml"))  # fmt: skip
    batch = Path(silver_path).stem
    if engine is not None:
        QualityScoreStore(engine).save(source, run_id, score, schema_version)
    jpath, hpath = write_report(
        build_report(run_id=run_id, source=source, generated_at=datetime.now(UTC),
                     quality=report, score=score, decision=decision),
        report_dir / source,
    )  # fmt: skip
    gold, skipped, quarantined = "", False, 0
    if verdict.status == "promoted":
        gold, skipped = _promote(gold_store, silver_store, source, batch, silver_path,
                                 len(rows), score.score, run_id)  # fmt: skip
    else:
        outcome = QuarantineStore(quarantine_store).apply(
            source, batch, columns, rows, report, config=qcfg, score=score, run_id=run_id
        )
        quarantined = outcome.quarantined
    log.info("promotion_gate", source=source, run_id=run_id, batch_id=batch,
             status=verdict.status, reasons=verdict.reasons, score=score.score)  # fmt: skip
    return PromotionResult(verdict.status, verdict.reasons, batch, score.score, len(rows),
                           quarantined, gold, str(jpath), str(hpath), skipped)  # fmt: skip
