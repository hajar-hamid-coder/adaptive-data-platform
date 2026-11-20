"""Recommendation rules (P6-05): pure and deterministic, fed by the aggregated usage journal.

Rules (thresholds in config/optimizer.yaml, section `advisor`):
- Evidence gate, for every rule: `observations >= min_observations` (raw, unweighted events),
  `p95 >= min_p95_ms` and `time_share >= min_time_share`. Below that, nothing is recommended.
- `materialize_indicator` (op `indicator`): the indicator is recomputed from every promoted
  Silver batch on each call; a pre-computed Parquet artefact (keyed by the inputs fingerprint)
  serves the same rows.
- `keyset_pagination` (op `page`): when deep pages (`offset >= deep_offset`) are at least
  `deep_share_min` of the requests, a derived artefact carrying the row position `_rn` with
  small row groups lets DuckDB prune row groups (zone maps) instead of scanning to the offset.
- Expected gain = `assumed_gain[type]` (x deep share for keyset); kept only when it reaches
  `min_expected_gain`. It is a prior, the before/after report measures the real gain.
- Not emitted, on purpose: Parquet re-sort by filter column and result caching. The API exposes
  no filter parameter (no filter column ever appears in the journal), and the immutable,
  fingerprinted indicator artefact already is a result cache that can never serve stale rows.
- A recommendation already applied (active in the optimisation journal) is not repeated.
- Stable id: `<mv|ks>-<target>-<sha256(shape key)[:8]>`, the same for the same usage shape.
"""

import hashlib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from adp.errors import ConfigError
from adp.optimizer.usage import CONFIG_PATH, ShapeStats, depth_min, load_section

RecType = Literal["materialize_indicator", "keyset_pagination"]
PREFIX: dict[str, str] = {"materialize_indicator": "mv", "keyset_pagination": "ks"}


class AdvisorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_observations: int = Field(30, ge=1)
    min_p95_ms: float = Field(50, ge=0)
    min_time_share: float = Field(0.10, ge=0, le=1)
    min_expected_gain: float = Field(0.30, ge=0, le=1)
    deep_offset: int = Field(10_000, ge=1)
    deep_share_min: float = Field(0.5, ge=0, le=1)
    assumed_gain: dict[RecType, float] = Field(
        default_factory=lambda: {"materialize_indicator": 0.8, "keyset_pagination": 0.5}  # type: ignore[arg-type]
    )


def load_advisor_config(path: Path = CONFIG_PATH) -> AdvisorConfig:
    try:
        return AdvisorConfig.model_validate(load_section("advisor", path))
    except ValidationError as exc:
        raise ConfigError(
            f"{path}: section advisor invalide ({exc.error_count()} erreur(s))"
        ) from None


class Recommendation(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    type: RecType
    target: str
    shape_key: str
    justification: str  # French, figures from the journal
    observations: int
    count: float
    p95_ms: float
    time_share: float
    expected_gain: float
    storage_bytes_est: int
    build_seconds_est: float
    risk: str


def rec_id(rec_type: RecType, target: str, shape_key: str) -> str:
    return f"{PREFIX[rec_type]}-{target}-{hashlib.sha256(shape_key.encode()).hexdigest()[:8]}"


def _deep_share(s: ShapeStats, deep_offset: int) -> float:
    return sum(v for b, v in s.deep_share.items() if depth_min(b) >= deep_offset)


def recommend(
    stats: Sequence[ShapeStats], cfg: AdvisorConfig, applied: Iterable[str] = ()
) -> list[Recommendation]:
    """Recommendations, highest share of total time first. Pure."""
    done = set(applied)
    out: list[Recommendation] = []
    for s in stats:
        if (s.observations < cfg.min_observations or s.p95 < cfg.min_p95_ms
                or s.time_share < cfg.min_time_share):  # fmt: skip
            continue
        rtype: RecType
        if s.op == "indicator":
            rtype, extra = "materialize_indicator", ""
            gain = cfg.assumed_gain.get(rtype, 0.0)
            storage = s.max_total * max(s.ncols, 1) * 8
            build = s.p50 / 1000
            risk = (
                "faible : artefact additif, ignoré dès que les lots promus ou le SQL changent "
                "(repli sur le calcul) ; coût : espace disque, reconstruction manuelle"
            )
        else:
            deep = _deep_share(s, cfg.deep_offset)
            if deep < cfg.deep_share_min:
                continue
            rtype = "keyset_pagination"
            gain = cfg.assumed_gain.get(rtype, 0.0) * deep
            extra = f", {deep:.0%} de pages profondes (offset ≥ {cfg.deep_offset})"
            storage = s.max_total * (max(s.ncols, 1) + 1) * 8
            build = 2 * s.p50 / 1000
            risk = ("faible : copie additive du Gold avec position de ligne, ignorée si la "
                    "version Gold change ; coût : espace disque")  # fmt: skip
        if gain < cfg.min_expected_gain:
            continue
        rid = rec_id(rtype, s.target, s.key)
        if rid in done:
            continue
        out.append(Recommendation(
            id=rid, type=rtype, target=s.target, shape_key=s.key, observations=s.observations,
            count=s.count, p95_ms=s.p95, time_share=s.time_share, expected_gain=round(gain, 3),
            storage_bytes_est=storage, build_seconds_est=round(build, 3), risk=risk,
            justification=(f"{s.observations} requêtes observées (≈ {s.count:.0f} pondérées), "
                           f"p95 = {s.p95:.1f} ms, {s.time_share:.0%} du temps total des "
                           f"requêtes analytiques{extra}"),
        ))  # fmt: skip
    return sorted(out, key=lambda r: (-r.time_share, r.id))
