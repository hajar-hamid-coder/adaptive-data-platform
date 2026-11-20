"""Compaction of small Parquet files (P6-06). DESTRUCTIVE-ADJACENT: read the guard rails.

Scope (decision): Bronze and Silver small files only. NEVER Gold (`gold/` artefacts are immutable
and content-addressed, `gold_ready/` batches and `gold_opt/` are lineage/serving inputs), never
anything under a `_`-prefixed root (archive, journals, this module's own state). Dead-letter
files are JSONL, not Parquet: out of scope. A file is a candidate only if ALL hold:
  - its sibling `<stem>.manifest.json` exists (the batch is complete: no in-flight write);
  - no other manifest references it (`referenced_keys`: Silver `bronze_batch`, Gold `silver_path`),
    so lineage never points into a merged file;
  - its partition `ingestion_date=` is older than `min_age_days` (blocked batches have time to be
    replayed from their own Bronze file); partitions without a date are never touched;
  - it is smaller than `small_bytes` and not itself a `compacted-*` output.

Protocol (`compact`, dry-run by default, nothing is ever deleted here):
 1. a reservation `_compaction/locks/<gid>` (put_if_absent; stale after `lock_ttl_seconds`);
    correctness never depends on it: every write below is put_if_absent and deterministic;
 2. identical schema (names, types, order) required, else the group is refused and reported;
 3. the merged file is written (DuckDB), read back AS STORED, and its row count and an
    order-insensitive row-multiset hash (sha256 of the sorted per-row sha256) must equal the
    sources' BEFORE anything is marked;
 4. data `compacted-<gid>.parquet`, then its manifest, then the record
    `_compaction/records/<gid>.json` (sources -> target): the record is the commit point.
Old files stay in place as `superseded` until the retention pass (retention.py) deletes them
after the grace delay.

Readers: `live_keys` / `superseded_keys` are THE listing to use when unioning files. Sources
named by a record whose target exists are hidden (with their manifests); a `compacted-*` file
without a record (crash before the commit point) is hidden too. So a reader never sees a row
twice, at any instant. Per-batch lookups (replay) address a batch by its own file and are
unaffected. Crash anywhere = consistent state: before the record the sources are the truth; after
it the target is; never a loss, a replay completes the missing steps (no-op if complete).
Idempotent: a second pass finds the sources superseded and plans nothing.
"""

import hashlib
import json
import os
import statistics
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from adp.bronze.store import ObjectStore
from adp.errors import ConfigError, OptimizerError
from adp.logging import get_logger

log = get_logger(__name__)

COMP = "_compaction"
RECORDS, LOCKS, REPORTS = f"{COMP}/records/", f"{COMP}/locks/", f"{COMP}/reports/"
FORBIDDEN_ROOTS = frozenset({"gold", "gold_ready", "gold_opt"})
LAYERS = ("bronze", "silver")
_DATE = "ingestion_date="


class CompactionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    small_bytes: int = Field(default=8 * 2**20, ge=1)
    target_bytes: int = Field(default=128 * 2**20, ge=1)
    min_files: int = Field(default=2, ge=2)
    min_age_days: int = Field(default=7, ge=0)
    lock_ttl_seconds: int = Field(default=3600, ge=1)
    schedule_apply: bool = False


def load_compaction_config(path: Path = Path("config/compaction.yaml")) -> CompactionConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return CompactionConfig.model_validate(raw or {})
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"{path}: invalid compaction config ({type(exc).__name__})") from exc


def disabled() -> bool:
    """Kill switch: ADP_COMPACTION_DISABLED=1 turns every apply into a no-op."""
    return os.environ.get("ADP_COMPACTION_DISABLED", "").strip() == "1"


@dataclass(frozen=True)
class Group:
    """One planned merge: ordered small files of ONE partition -> one target file."""

    gid: str
    partition: str
    sources: tuple[str, ...]
    sizes: tuple[int, ...]
    target: str

    @property
    def source_name(self) -> str:
        return self.partition.split("/", 1)[0]


@dataclass
class GroupResult:
    gid: str
    source: str
    status: str  # planned|compacted|noop|locked|refused_schema|verify_failed|missing|disabled
    files_before: int = 0
    files_after: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    rows_before: int = 0
    rows_after: int = 0
    rows_verified: int = 0
    detail: str = ""
    sizes_before: list[int] = field(default_factory=list)


@dataclass
class CompactionReport:
    layer: str
    dry_run: bool
    results: list[GroupResult]
    duration_s: float

    def stats(self) -> dict[str, dict[str, Any]]:
        """Per source: files/bytes/median before-after (groups that are, or would be, merged)."""
        out: dict[str, dict[str, Any]] = {}
        for r in self.results:
            if r.status not in ("compacted", "planned", "noop"):
                continue
            s = out.setdefault(r.source, {"files_before": 0, "files_after": 0, "bytes_before": 0,
                                          "bytes_after": 0, "rows_before": 0, "rows_after": 0,
                                          "rows_verified": 0, "_sb": [], "_sa": []})  # fmt: skip
            for k in ("files_before", "files_after", "bytes_before", "bytes_after",
                      "rows_before", "rows_after", "rows_verified"):  # fmt: skip
                s[k] += getattr(r, k)
            s["_sb"] += r.sizes_before
            if r.bytes_after:
                s["_sa"].append(r.bytes_after)
        for s in out.values():
            s["median_before"] = int(statistics.median(s.pop("_sb") or [0]))
            s["median_after"] = int(statistics.median(s.pop("_sa") or [0]))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"layer": self.layer, "dry_run": self.dry_run,
                "duration_s": round(self.duration_s, 3),
                "groups": [asdict(r) | {"sizes_before": None} for r in self.results],
                "sources": self.stats()}  # fmt: skip

    def markdown(self) -> str:
        head = "| source | statut | fichiers | taille (o) | lignes | vérifiées (hash) |\n|---|---|---|---|---|---|\n"  # noqa: E501
        rows = "".join(
            f"| {r.source} | {r.status} | {r.files_before} -> {r.files_after} | "
            f"{r.bytes_before} -> {r.bytes_after} | {r.rows_before} -> {r.rows_after} | "
            f"{r.rows_verified} |\n" for r in self.results)  # fmt: skip
        mode = "SIMULATION" if self.dry_run else "APPLIQUÉ"
        return (f"### Compaction {self.layer} ({mode}, {self.duration_s:.2f} s)\n\n"
                f"{head}{rows}")  # fmt: skip


# --- state and readers -------------------------------------------------------------------------
def _manifest_of(key: str) -> str:
    return key.removesuffix(".parquet") + ".manifest.json"


def _records(store: ObjectStore) -> list[dict[str, Any]]:
    out = []
    for k in store.list(RECORDS):
        if k.endswith(".json"):
            try:
                out.append(json.loads(store.get(k)))
            except ValueError:
                log.warning("compaction_record_unreadable")  # fail closed: nothing is hidden
    return out


def _valid(store: ObjectStore, rec: dict[str, Any]) -> bool:
    return store.exists(rec["target"]) and store.exists(_manifest_of(rec["target"]))


def superseded_keys(store: ObjectStore) -> frozenset[str]:
    """Source files (data and manifest) replaced by a committed compaction."""
    out: set[str] = set()
    for rec in _records(store):
        if _valid(store, rec):
            for s in rec["sources"]:
                out |= {s, _manifest_of(s)}
    return frozenset(out)


def live_keys(store: ObjectStore, prefix: str = "") -> list[str]:
    """Keys a UNION reader must use: no superseded source, no uncommitted `compacted-*` file,
    no internal `_compaction/` state. Each logical row is in exactly one live file."""
    recs = [r for r in _records(store) if _valid(store, r)]
    hidden = {k for r in recs for s in r["sources"] for k in (s, _manifest_of(s))}
    committed = {r["target"].removesuffix(".parquet") for r in recs}
    out = []
    for k in store.list(prefix):
        base = k.rsplit("/", 1)[-1]
        if k in hidden or k.startswith(COMP + "/"):
            continue
        if base.startswith("compacted-") and k.split(".", 1)[0] not in committed:
            continue
        out.append(k)
    return out


def referenced_keys(*stores: ObjectStore) -> frozenset[str]:
    """Keys that another manifest points to (Silver `bronze_batch`, Gold `silver_path`)."""
    out: set[str] = set()
    for st in stores:
        for k in st.list():
            if not k.endswith("manifest.json"):
                continue
            try:
                doc = json.loads(st.get(k))
            except ValueError:
                continue
            if isinstance(doc, dict):
                out |= {v for f in ("bronze_batch", "silver_path")
                        if isinstance(v := doc.get(f), str)}  # fmt: skip
    return frozenset(out)


# --- plan (pure) -------------------------------------------------------------------------------
def _check_prefix(prefix: str) -> None:
    parts = prefix.split("/")
    if (prefix.startswith("/") or ".." in parts or "\\" in prefix or "\x00" in prefix
            or parts[0] in FORBIDDEN_ROOTS or parts[0].startswith(("_", "."))):  # fmt: skip
        raise OptimizerError(f"compaction: préfixe refusé '{prefix}'")


def _partition_date(partition: str) -> date | None:
    for seg in partition.split("/"):
        if seg.startswith(_DATE):
            try:
                return date.fromisoformat(seg.removeprefix(_DATE))
            except ValueError:
                return None
    return None


def _group(part: str, cur: list[tuple[str, int]], config: CompactionConfig) -> list[Group]:
    if len(cur) < config.min_files:
        return []
    srcs = tuple(k for k, _ in cur)
    gid = hashlib.sha256("\n".join(srcs).encode()).hexdigest()[:16]
    return [Group(gid, part, srcs, tuple(s for _, s in cur), f"{part}/compacted-{gid}.parquet")]


def plan_compaction(
    store: ObjectStore, prefix: str, config: CompactionConfig, *, today: date | None = None,
    protected: frozenset[str] = frozenset(),
) -> list[Group]:  # fmt: skip
    """Deterministic plan from the listed state: groups of small candidate files per partition,
    stable order (sorted keys), packed up to `target_bytes`, at least `min_files` each."""
    _check_prefix(prefix)
    today = today or datetime.now(UTC).date()
    cutoff = today - timedelta(days=config.min_age_days)
    keys = store.list(prefix)
    have = set(keys)
    hidden = superseded_keys(store)
    by_part: dict[str, list[str]] = {}
    for k in keys:
        base = k.rsplit("/", 1)[-1]
        part = k.rsplit("/", 1)[0] if "/" in k else ""
        segs = k.split("/")
        d = _partition_date(part)
        if (not k.endswith(".parquet") or base.startswith(("compacted-", ".")) or not part
                or any(s.startswith(("_", ".")) for s in segs) or segs[0] in FORBIDDEN_ROOTS
                or k in hidden or k in protected or _manifest_of(k) not in have
                or d is None or d >= cutoff):  # fmt: skip
            continue
        by_part.setdefault(part, []).append(k)
    groups: list[Group] = []
    for part in sorted(by_part):
        cur: list[tuple[str, int]] = []
        total = 0
        for k in sorted(by_part[part]):
            size = len(store.get(k))
            if size >= config.small_bytes:
                continue
            if cur and total + size > config.target_bytes:
                groups += _group(part, cur, config)
                cur, total = [], 0
            cur.append((k, size))
            total += size
        groups += _group(part, cur, config)
    return groups


# --- execution ---------------------------------------------------------------------------------
def _lit(p: Path) -> str:
    return "'" + str(p).replace("'", "''") + "'"


def _digest(con: duckdb.DuckDBPyConnection, files: list[Path]) -> tuple[int, str]:
    """(rows, hash of the row MULTISET): insensitive to row and file order."""
    lst = "[" + ",".join(_lit(f) for f in files) + "]"
    row = con.execute(
        "SELECT count(*), sha256(coalesce(string_agg(h, '' ORDER BY h), '')) FROM "
        f"(SELECT sha256(CAST(t AS VARCHAR)) AS h FROM read_parquet({lst}) t)"
    ).fetchone()
    assert row is not None
    return int(row[0]), str(row[1])


def _schema(con: duckdb.DuckDBPyConnection, f: Path) -> tuple[tuple[str, str], ...]:
    rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet({_lit(f)})").fetchall()
    return tuple((str(r[0]), str(r[1])) for r in rows)


def _acquire(store: ObjectStore, gid: str, ttl: int, now: datetime) -> bool:
    key = f"{LOCKS}{gid}.json"
    body = json.dumps({"at": now.isoformat()}).encode()
    if store.put_if_absent(key, body):
        return True
    try:
        stale = (now - datetime.fromisoformat(json.loads(store.get(key))["at"])).total_seconds()
    except (ValueError, KeyError, OSError):
        return False  # unreadable lock: fail closed
    if stale <= ttl:
        return False
    store.delete(key)  # crashed holder; two breakers is safe (puts are idempotent)
    return store.put_if_absent(key, body)


def _compact_group(
    store: ObjectStore, g: Group, cfg: CompactionConfig, dry_run: bool, now: datetime,
    by: str, reason: str,
) -> GroupResult:  # fmt: skip
    res = GroupResult(g.gid, g.source_name, "planned" if dry_run else "compacted",
                      files_before=len(g.sources), bytes_before=sum(g.sizes),
                      sizes_before=list(g.sizes))  # fmt: skip
    rec_key = f"{RECORDS}{g.gid}.json"
    if store.exists(rec_key):
        res.status = "noop"
        return res
    if not all(store.exists(s) for s in g.sources):
        res.status, res.detail = "missing", "a source file is gone"
        return res
    held = not dry_run and not _acquire(store, g.gid, cfg.lock_ttl_seconds, now)
    if held:
        res.status = "locked"
        return res
    try:
        if not dry_run and store.exists(rec_key):  # a concurrent run committed meanwhile
            res.status = "noop"
            return res
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for i, s in enumerate(g.sources):
                f = Path(tmp) / f"s{i:05d}.parquet"
                f.write_bytes(store.get(s))
                files.append(f)
            con = duckdb.connect()  # local temp files only; no SQL from outside is ever run
            try:
                schemas = {_schema(con, f) for f in files}
                if len(schemas) != 1:
                    res.status, res.detail = "refused_schema", "incompatible schemas in group"
                    log.warning("compaction_refused", gid=g.gid, reason="schema",
                                source=g.source_name)  # fmt: skip
                    return res
                rows, h = _digest(con, files)
                res.rows_before = rows
                if dry_run:
                    res.files_after = 1
                    return res
                dest = Path(tmp) / "out.parquet"
                lst = "[" + ",".join(_lit(f) for f in files) + "]"
                con.execute(f"COPY (SELECT * FROM read_parquet({lst})) TO {_lit(dest)} "
                            "(FORMAT PARQUET)")  # fmt: skip
                blob = dest.read_bytes()
                if not store.put_if_absent(g.target, blob):  # earlier attempt: verify its bytes
                    blob = store.get(g.target)
                stored = Path(tmp) / "stored.parquet"
                stored.write_bytes(blob)
                rows_after, h_after = _digest(con, [stored])
            finally:
                con.close()
        if (rows_after, h_after) != (rows, h):
            res.status, res.detail = "verify_failed", "row count or hash differs; nothing marked"
            log.error("compaction_verify_failed", gid=g.gid, source=g.source_name)
            return res
        res.rows_after = res.rows_verified = rows_after
        res.files_after, res.bytes_after = 1, len(blob)
        manifest = {"kind": "compacted", "gid": g.gid, "rows": rows, "row_hash": h,
                    "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob),
                    "parquet_path": g.target, "sources": list(g.sources)}  # fmt: skip
        store.put_if_absent(_manifest_of(g.target), json.dumps(manifest, indent=2).encode())
        record = manifest | {"created_at": now.isoformat(), "by": by, "reason": reason,
                             "bytes_before": res.bytes_before, "target": g.target}  # fmt: skip
        store.put_if_absent(rec_key, json.dumps(record, indent=2).encode())  # commit point
        log.info("compaction_done", gid=g.gid, source=g.source_name, files=len(g.sources),
                 rows=rows)  # fmt: skip
        return res
    finally:
        if not dry_run and not held:
            store.delete(f"{LOCKS}{g.gid}.json")


def compact(
    plan: list[Group], store: ObjectStore, *, dry_run: bool = True,
    config: CompactionConfig | None = None, layer: str = "silver", now: datetime | None = None,
    by: str = "", reason: str = "",
) -> CompactionReport:  # fmt: skip
    """Execute a plan group by group. Dry-run (default) reads but writes nothing."""
    cfg = config or CompactionConfig()
    if layer not in LAYERS:
        raise OptimizerError(f"couche inconnue '{layer}'")
    t0 = time.perf_counter()
    now = now or datetime.now(UTC)
    if not dry_run:
        if not (by.strip() and reason.strip()):
            raise OptimizerError("compaction --apply : --by et --reason obligatoires")
        if disabled() or not cfg.enabled:
            log.warning("compaction_disabled")
            res = [GroupResult(g.gid, g.source_name, "disabled", files_before=len(g.sources))
                   for g in plan]  # fmt: skip
            return CompactionReport(layer, False, res, time.perf_counter() - t0)
    results = []
    for g in plan:
        try:
            results.append(_compact_group(store, g, cfg, dry_run, now, by, reason))
        except Exception as exc:  # one bad group never stops the others (nothing is half-marked)
            log.error("compaction_group_failed", gid=g.gid, error=type(exc).__name__)
            results.append(GroupResult(g.gid, g.source_name, "verify_failed",
                                       detail=type(exc).__name__))  # fmt: skip
    report = CompactionReport(layer, dry_run, results, time.perf_counter() - t0)
    if any(r.status == "compacted" for r in results):
        write_reports(store, report, now)
    return report


def write_reports(store: ObjectStore, report: CompactionReport, now: datetime) -> None:
    """One immutable JSON per (layer, source) and pass; the Prometheus collector reads the last."""
    for source, st in report.stats().items():
        key = f"{REPORTS}{report.layer}/{source}/{now:%Y%m%dT%H%M%S%f}-{uuid.uuid4().hex[:6]}.json"
        body = {"layer": report.layer, "source": source, "at": now.isoformat(),
                "duration_s": round(report.duration_s, 3)} | st  # fmt: skip
        store.put_if_absent(key, json.dumps(body).encode())


def latest_reports(store: ObjectStore) -> list[dict[str, Any]]:
    """Latest report per (layer, source), chosen from key names (reads only those)."""
    best: dict[tuple[str, str], str] = {}
    for k in store.list(REPORTS):
        parts = k.removeprefix(REPORTS).split("/")
        if len(parts) == 3 and parts[0] in LAYERS:
            best[(parts[0], parts[1])] = max(best.get((parts[0], parts[1]), ""), k)
    return [json.loads(store.get(k)) for k in best.values()]


def run_layers(
    layers: dict[str, ObjectStore], protect_from: list[ObjectStore], config: CompactionConfig,
    *, apply: bool = False, today: date | None = None, by: str = "", reason: str = "",
) -> list[CompactionReport]:  # fmt: skip
    """Plan and run every layer (`bronze`, `silver`); dry-run unless `apply`."""
    protected = referenced_keys(*protect_from)
    out = []
    for layer, store in layers.items():
        plan = plan_compaction(store, "", config, today=today, protected=protected)
        out.append(compact(plan, store, dry_run=not apply, config=config, layer=layer,
                           by=by, reason=reason))  # fmt: skip
    return out
