"""Human-approved application of a recommendation (P6-05) and optimised serving.

Guard rails:
- Never automatic: only `apply(..., by, reason)` (CLI `adp optimize apply`) activates an
  optimisation; `by` and `reason` are mandatory, journaled append-only as one object per event
  under `gold_opt/_journal/` (`put_if_absent`, works on S3/MinIO as on disk). The state of a
  recommendation is its LAST event (apply/revert).
- Additive only: artefacts are written under `gold_opt/` with `put_if_absent`; Gold and Silver
  sources are only read. Revert is a journal event (the immutable artefact simply stops being
  used), so it is instantaneous and itself reversible.
- Freshness: an artefact path embeds the fingerprint of its inputs (indicator: SQL text + the
  promoted batch keys of its sources; product: the Gold `sha256`). A newer input = another path
  that does not exist yet = the stale artefact is ignored and the source is served (correct but
  slower) until the human re-applies (idempotent, rebuilds for the new inputs). Never stale rows.
- Equivalence: before activating, the full result and sample pages served through the
  optimised path are compared with the source path (floats at 1e-12 relative, because DuckDB
  parallel float aggregates are not associative); a mismatch refuses the application.
- Idempotence: apply on an active recommendation whose artefact is fresh = no-op (no event);
  concurrent applies converge (put_if_absent; the manifest describes the bytes actually stored).
- Kill switch: `ADP_OPTIMIZER_DISABLED=1` makes serving ignore every artefact.
"""

import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import duckdb
from pydantic import BaseModel

from adp.bronze.store import ObjectStore
from adp.errors import OptimizerError
from adp.gold.views import SQL_DIR, indicator_page, indicator_sql, indicator_table, latest_gold
from adp.logging import get_logger
from adp.optimizer.advisor import RecType
from adp.orchestration.promotion_gate import GOLD_PREFIX

log = get_logger(__name__)

OPT_PREFIX = "gold_opt"
JOURNAL = f"{OPT_PREFIX}/_journal/"
ROW_GROUP = 8192
_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_ID = re.compile(r"^(mv|ks)-[a-z][a-z0-9_]{0,63}-[0-9a-f]{8}$")
Rows = list[tuple[Any, ...]]


class OptEvent(BaseModel):
    action: Literal["apply", "revert"]
    rec_id: str
    type: RecType
    target: str
    by: str
    reason: str
    at: datetime


@dataclass(frozen=True)
class ApplyResult:
    status: Literal["applied", "refreshed", "noop"]
    base: str
    bytes: int


def disabled() -> bool:
    return os.environ.get("ADP_OPTIMIZER_DISABLED", "").strip() == "1"


def _text(name: str, value: str) -> str:
    v = (value or "").strip()
    if not _TEXT.fullmatch(v):
        raise OptimizerError(f"--{name} obligatoire (1 à 200 caractères imprimables)")
    return v


def journal(store: ObjectStore) -> list[OptEvent]:
    return [OptEvent.model_validate_json(store.get(k)) for k in store.list(JOURNAL)
            if k.endswith(".json")]  # fmt: skip


def active(store: ObjectStore) -> dict[str, OptEvent]:
    """rec_id -> its apply event, for recommendations whose last event is `apply`."""
    last: dict[str, OptEvent] = {}
    for e in journal(store):  # keys sort by timestamp
        last[e.rec_id] = e
    return {k: e for k, e in last.items() if e.action == "apply"}


def _record(store: ObjectStore, ev: OptEvent) -> None:
    key = f"{JOURNAL}{ev.at:%Y%m%dT%H%M%S%f}-{uuid.uuid4().hex[:8]}.json"
    store.put_if_absent(key, ev.model_dump_json().encode())


def indicator_fingerprint(store: ObjectStore, name: str, sql_dir: Path = SQL_DIR) -> str:
    sql, sources = indicator_sql(name, sql_dir=sql_dir)
    inputs = {s: store.list(f"{GOLD_PREFIX}/{s}/") for s in sources}
    doc = {"sql": hashlib.sha256(sql.encode()).hexdigest(), "inputs": inputs}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def _base(rtype: str, target: str, rid: str, fp: str) -> str:
    kind = "indicators" if rtype == "materialize_indicator" else "products"
    return f"{OPT_PREFIX}/{kind}/{target}/{rid}/{fp[:16]}"


def _fresh(store: ObjectStore, rtype: str, target: str, fp: str) -> str | None:
    """Data key of a fresh artefact of an ACTIVE recommendation, if any."""
    if disabled():
        return None
    for rid, e in sorted(active(store).items()):
        if e.type == rtype and e.target == target:
            mk = f"{_base(rtype, target, rid, fp)}/manifest.json"
            if store.exists(mk) and json.loads(store.get(mk)).get("source_fp") == fp:
                return mk.removesuffix("manifest.json") + "data.parquet"
    return None


def current_artefact(
    store: ObjectStore, rtype: str, target: str, sql_dir: Path = SQL_DIR
) -> str | None:
    """Data key of the fresh artefact of an active recommendation for `target`, if any."""
    if rtype == "materialize_indicator":
        return _fresh(store, rtype, target, indicator_fingerprint(store, target, sql_dir))
    latest = latest_gold(store, target)
    return _fresh(store, rtype, target, latest[0]["sha256"]) if latest else None


def read_page(
    store: ObjectStore, key: str, limit: int, offset: int, *, keyset: bool = False
) -> tuple[list[str], Rows, int]:
    """Page of a Parquet object: LIMIT/OFFSET, or a range on `_rn` (row-group pruning)."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "d.parquet"
        f.write_bytes(store.get(key))
        con = duckdb.connect()
        try:
            total = con.execute("SELECT count(*) FROM read_parquet(?)", [str(f)]).fetchone()
            if keyset:
                cur = con.execute(
                    "SELECT * EXCLUDE (_rn) FROM read_parquet(?) WHERE _rn >= ? AND _rn < ? "
                    "ORDER BY _rn", [str(f), offset, offset + limit],
                )  # fmt: skip
            else:
                cur = con.execute(
                    "SELECT * FROM read_parquet(?) LIMIT ? OFFSET ?", [str(f), limit, offset]
                )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        finally:
            con.close()
    return cols, rows, int(total[0]) if total else 0


def serve_indicator(
    store: ObjectStore, name: str, limit: int, offset: int, *, use_opt: bool = True,
    sql_dir: Path = SQL_DIR,
) -> tuple[list[str], Rows, int, bool]:  # fmt: skip
    """Indicator page: materialised artefact when active and fresh, else computed. Last item:
    True when the optimised artefact served the request."""
    if use_opt:
        key = _fresh(store, "materialize_indicator", name,
                     indicator_fingerprint(store, name, sql_dir))  # fmt: skip
        if key:
            return (*read_page(store, key, limit, offset), True)
    return (*indicator_page(name, store, limit, offset, sql_dir=sql_dir), False)


def serve_product(
    store: ObjectStore, name: str, manifest: dict[str, Any], data_key: str, limit: int,
    offset: int, *, use_opt: bool = True,
) -> tuple[list[str], Rows, int, bool]:  # fmt: skip
    """Gold product page: keyset artefact when active and fresh, else LIMIT/OFFSET."""
    key = _fresh(store, "keyset_pagination", name, manifest["sha256"]) if use_opt else None
    if key:
        return (*read_page(store, key, limit, offset, keyset=True), True)
    return (*read_page(store, data_key, limit, offset), False)


def same_rows(a: Rows, b: Rows) -> bool:
    """Exact equality, floats at 1e-12 relative (pure)."""
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b, strict=True):
        if len(ra) != len(rb):
            return False
        for x, y in zip(ra, rb, strict=True):
            if isinstance(x, float) and isinstance(y, float):
                if not (x == y or (math.isnan(x) and math.isnan(y))
                        or math.isclose(x, y, rel_tol=1e-12)):  # fmt: skip
                    return False
            elif x != y:
                return False
    return True


def _build(store: ObjectStore, rtype: str, target: str, rid: str, fp: str,
           sql_dir: Path, src_key: str | None) -> tuple[bool, str, int]:  # fmt: skip
    base = _base(rtype, target, rid, fp)
    if store.exists(f"{base}/manifest.json"):
        return False, base, len(store.get(f"{base}/data.parquet"))
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "out.parquet"
        db = str(Path(tmp) / "w.duckdb")
        if rtype == "materialize_indicator":
            con = duckdb.connect(db)  # file DB: the sandboxed connection cannot COPY
            try:
                indicator_table(con, target, store, Path(tmp), sql_dir=sql_dir)
            finally:
                con.close()
            con = duckdb.connect(db)
            try:
                con.execute(f"COPY q TO '{dest}' (FORMAT PARQUET)")
            finally:
                con.close()
            if indicator_fingerprint(store, target, sql_dir) != fp:
                raise OptimizerError("entrées modifiées pendant la construction : relancer")
        else:
            assert src_key is not None
            src = Path(tmp) / "src.parquet"
            src.write_bytes(store.get(src_key))
            con = duckdb.connect()
            try:
                con.execute(
                    f"COPY (SELECT * EXCLUDE (file_row_number), file_row_number AS _rn "
                    f"FROM read_parquet('{src}', file_row_number=true) ORDER BY file_row_number) "
                    f"TO '{dest}' (FORMAT PARQUET, ROW_GROUP_SIZE {ROW_GROUP})"
                )
            finally:
                con.close()
        store.put_if_absent(f"{base}/data.parquet", dest.read_bytes())
    stored = store.get(f"{base}/data.parquet")  # a concurrent builder may have won
    con = duckdb.connect()
    try:
        with tempfile.NamedTemporaryFile(suffix=".parquet") as f:
            f.write(stored)
            f.flush()
            row = con.execute("SELECT count(*) FROM read_parquet(?)", [f.name]).fetchone()
    finally:
        con.close()
    manifest = {
        "rec_id": rid, "type": rtype, "target": target, "source_fp": fp,
        "rows": int(row[0]) if row else 0, "sha256": hashlib.sha256(stored).hexdigest(),
        "bytes": len(stored), "built_at": datetime.now(UTC).isoformat(),
    }  # fmt: skip
    created = store.put_if_absent(f"{base}/manifest.json", json.dumps(manifest, indent=2).encode())
    return created, base, len(stored)


def check_equivalence(
    store: ObjectStore, rtype: str, target: str, base: str, sql_dir: Path = SQL_DIR,
    page: int = 50,
) -> bool:  # fmt: skip
    """Full result and sample pages: optimised artefact vs source path."""
    data_key = f"{base}/data.parquet"
    keyset = rtype == "keyset_pagination"
    if keyset:
        latest = latest_gold(store, target)
        if latest is None:
            return False
        _, src_rows, total = read_page(store, latest[1], 2**62, 0)
    else:
        _, src_rows, total = indicator_page(target, store, 2**62, 0, sql_dir=sql_dir)
    _, opt_rows, opt_total = read_page(store, data_key, 2**62, 0, keyset=keyset)
    if total != opt_total or not same_rows(src_rows, opt_rows):
        return False
    for off in sorted({0, total // 2, max(total - page, 0), total + 1}):
        _, rows, _ = read_page(store, data_key, page, off, keyset=keyset)
        if not same_rows(rows, src_rows[off : off + page]):
            return False
    return True


def apply(
    store: ObjectStore, rid: str, rtype: RecType, target: str, by: str, reason: str,
    *, sql_dir: Path = SQL_DIR, now: datetime | None = None,
) -> ApplyResult:  # fmt: skip
    """Build (if needed), verify equivalence, then activate. Human-only entry point."""
    by, reason = _text("by", by), _text("reason", reason)
    if (
        not _ID.fullmatch(rid)
        or rid.split("-", 1)[0] != {"materialize_indicator": "mv", "keyset_pagination": "ks"}[rtype]
    ):
        raise OptimizerError("identifiant de recommandation invalide")
    src_key = None
    if rtype == "materialize_indicator":
        fp = indicator_fingerprint(store, target, sql_dir)
    else:
        latest = latest_gold(store, target)
        if latest is None:
            raise OptimizerError(f"aucune version Gold pour '{target}'")
        fp, src_key = latest[0]["sha256"], latest[1]
    was_active = rid in active(store)
    created, base, size = _build(store, rtype, target, rid, fp, sql_dir, src_key)
    if was_active and not created:
        return ApplyResult("noop", base, size)
    if not check_equivalence(store, rtype, target, base, sql_dir):
        raise OptimizerError(f"{rid} : résultats non équivalents, application refusée")
    if was_active:
        log.info("optimizer_refreshed", rec_id=rid, base=base)
        return ApplyResult("refreshed", base, size)
    _record(store, OptEvent(action="apply", rec_id=rid, type=rtype, target=target, by=by,
                            reason=reason, at=now or datetime.now(UTC)))  # fmt: skip
    log.info("optimizer_applied", rec_id=rid, base=base, bytes=size)
    return ApplyResult("applied", base, size)


def revert(
    store: ObjectStore, rid: str, by: str, reason: str, *, now: datetime | None = None
) -> bool:
    """Deactivate (journal event, artefacts kept). False when it was not active (no-op)."""
    by, reason = _text("by", by), _text("reason", reason)
    ev = active(store).get(rid)
    if ev is None:
        return False
    _record(store, ev.model_copy(update={"action": "revert", "by": by, "reason": reason,
                                         "at": now or datetime.now(UTC)}))  # fmt: skip
    log.info("optimizer_reverted", rec_id=rid)
    return True
