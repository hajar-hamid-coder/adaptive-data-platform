"""Usage journal of analytical API requests (P6-05) and its pure aggregation.

Decisions:
- Storage: append-only JSONL file (`<dir>/usage.jsonl`), not a SQL table. It sits on the API hot
  path, must work without the metadata DB (`Deps.audit` may be None) and is read only in bulk by
  the advisor. One `os.write` of one line (< 1 KiB) on an `O_APPEND` descriptor per request:
  concurrent writers (threads, uvicorn workers) never interleave lines on a local filesystem.
- Rotation: beyond `max_bytes`, `os.replace` to `usage.<utc>.<uuid8>.jsonl` (a racing rotator
  simply loses the race); only the `max_files` newest rotated files are kept (retention cap).
- Privacy (H-09 spirit): an event holds the route TEMPLATE, the product/indicator name, the
  SHAPE of the query (filter/sort/aggregate column names, operation), duration, row counts,
  page size and a coarse depth BUCKET of the offset. Never a parameter value, a raw path, a
  query string, a header or a row value. Free text is refused by the model (identifier regex).
- Sampling: `sample_rate` per event; frequencies are re-weighted by 1/sample_rate, percentiles
  use the sampled durations as they are.
- Journaling never breaks a request: `UsageLog.record` swallows OSError and logs a warning.
"""

import json
import math
import os
import random
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from adp.errors import ConfigError
from adp.logging import get_logger

log = get_logger(__name__)

CONFIG_PATH = Path(__file__).parents[3] / "config" / "optimizer.yaml"
Ident = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Op = Literal["page", "indicator"]
Depth = Literal["0", "<1k", "<10k", "<100k", ">=100k"]
DEPTHS: tuple[tuple[int, Depth], ...] = (
    (0, "0"),
    (1_000, "<1k"),
    (10_000, "<10k"),
    (100_000, "<100k"),
)


class UsageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dir: Path = Path("data/usage")
    sample_rate: float = Field(1.0, ge=0.0, le=1.0)
    max_bytes: int = Field(5_000_000, ge=1_000)
    max_files: int = Field(5, ge=1)


def load_section(name: str, path: Path = CONFIG_PATH) -> dict[str, object]:
    """One top-level section of config/optimizer.yaml (missing file -> defaults)."""
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: YAML invalide") from exc
    section = data.get(name, {}) if isinstance(data, dict) else None
    if not isinstance(section, dict):
        raise ConfigError(f"{path}: section '{name}' invalide")
    return section


def load_usage_config(path: Path = CONFIG_PATH) -> UsageConfig:
    try:
        return UsageConfig.model_validate(load_section("usage", path))
    except ValidationError as exc:
        raise ConfigError(
            f"{path}: section usage invalide ({exc.error_count()} erreur(s))"
        ) from None


def depth_bucket(offset: int) -> Depth:
    """Coarse bucket of an offset (the value itself is never journaled)."""
    if offset <= 0:
        return "0"
    return next((label for bound, label in DEPTHS[1:] if offset < bound), ">=100k")


def depth_min(bucket: str) -> int:
    """Smallest offset of a bucket (for rules and replayed workloads)."""
    return {"0": 0, "<1k": 1, "<10k": 1_000, "<100k": 10_000, ">=100k": 100_000}[bucket]


class QueryShape(BaseModel):
    """What a request asked for, without any value."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    op: Op
    target: Ident
    filters: tuple[Ident, ...] = ()
    sort: tuple[Ident, ...] = ()
    agg: tuple[Ident, ...] = ()
    depth: Depth = "0"
    limit: int = Field(0, ge=0, le=10_000)

    def key(self, route: str) -> str:
        """Grouping key: route + operation + target + columns (depth and page size excluded)."""
        cols = "|".join(",".join(c) for c in (self.filters, self.sort, self.agg))
        return f"{route} {self.op}:{self.target}|{cols}"


class UsageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ts: datetime
    route: Annotated[str, Field(pattern=r"^/[A-Za-z0-9_/{}-]{0,127}$")]
    shape: QueryShape
    status: int = Field(ge=100, le=599)
    ms: float = Field(ge=0)
    rows: int = Field(ge=0)  # rows returned
    total: int = Field(ge=0)  # rows of the full result (a count)
    ncols: int = Field(ge=0)
    sample_rate: float = Field(gt=0, le=1)


class UsageLog:
    """Append-only, rotated, sampled JSONL journal."""

    def __init__(self, cfg: UsageConfig, rand: Callable[[], float] = random.random) -> None:
        self.cfg, self._rand = cfg, rand
        self.path = cfg.dir / "usage.jsonl"

    def record(self, event: UsageEvent) -> bool:
        """Append one event if sampled; False when skipped or on I/O error (never raises)."""
        if self.cfg.sample_rate <= 0 or self._rand() >= self.cfg.sample_rate:
            return False
        line = event.model_copy(update={"sample_rate": self.cfg.sample_rate})
        data = (line.model_dump_json() + "\n").encode()
        try:
            self.cfg.dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
            try:
                os.write(fd, data)
                size = os.fstat(fd).st_size
            finally:
                os.close(fd)
            if size >= self.cfg.max_bytes:
                self._rotate()
            return True
        except OSError as exc:
            log.warning("usage_log_failed", error=type(exc).__name__)
            return False

    def _rotate(self) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        try:
            os.replace(self.path, self.cfg.dir / f"usage.{stamp}.{uuid.uuid4().hex[:8]}.jsonl")
        except FileNotFoundError:
            return  # another writer rotated first
        for old in self.rotated()[: -self.cfg.max_files]:
            old.unlink(missing_ok=True)

    def rotated(self) -> list[Path]:
        return sorted(self.cfg.dir.glob("usage.*.jsonl"))

    def events(self) -> list[UsageEvent]:
        """Every readable event (rotated files first); malformed lines are skipped."""
        out: list[UsageEvent] = []
        for f in [*self.rotated(), self.path]:
            if not f.is_file():
                continue
            for raw in f.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(UsageEvent.model_validate(json.loads(raw)))
                except (ValueError, ValidationError):
                    continue
        return out


class ShapeStats(BaseModel):
    """Aggregated usage of one query shape."""

    key: str
    route: str
    op: Op
    target: str
    filters: tuple[str, ...]
    sort: tuple[str, ...]
    agg: tuple[str, ...]
    observations: int  # sampled events
    count: float  # re-weighted request count
    p50: float
    p95: float
    p99: float
    total_ms: float  # re-weighted
    time_share: float
    deep_share: dict[str, float]  # bucket -> share of requests
    limit: int  # most frequent page size
    max_total: int
    ncols: int


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in ]0, 100]); 0.0 for an empty sequence."""
    if not values:
        return 0.0
    s = sorted(values)
    return s[max(1, math.ceil(q / 100 * len(s))) - 1]


def aggregate(events: Iterable[UsageEvent]) -> list[ShapeStats]:
    """Successful (status < 400) events grouped by shape, most frequent first (then key)."""
    groups: dict[str, list[UsageEvent]] = defaultdict(list)
    for e in events:
        if e.status < 400:
            groups[e.shape.key(e.route)].append(e)
    grand = sum(e.ms / e.sample_rate for g in groups.values() for e in g) or 1.0
    out = []
    for key, g in groups.items():
        ms = [e.ms for e in g]
        weight = sum(1 / e.sample_rate for e in g)
        depth: dict[str, float] = defaultdict(float)
        for e in g:
            depth[e.shape.depth] += 1 / e.sample_rate / weight
        limits: dict[int, int] = defaultdict(int)
        for e in g:
            limits[e.shape.limit] += 1
        s = g[0].shape
        total_ms = sum(e.ms / e.sample_rate for e in g)
        out.append(ShapeStats(
            key=key, route=g[0].route, op=s.op, target=s.target, filters=s.filters, sort=s.sort,
            agg=s.agg, observations=len(g), count=round(weight, 3), p50=percentile(ms, 50),
            p95=percentile(ms, 95), p99=percentile(ms, 99), total_ms=round(total_ms, 3),
            time_share=round(total_ms / grand, 4), deep_share=dict(sorted(depth.items())),
            limit=max(sorted(limits), key=lambda k: limits[k]),
            max_total=max(e.total for e in g), ncols=max(e.ncols for e in g),
        ))  # fmt: skip
    return sorted(out, key=lambda x: (-x.count, x.key))
