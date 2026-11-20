"""Benchmark P6-05 / H-11: p95 latency of the most frequent API query, before vs after the
recommended (and explicitly applied) optimisation, on a synthetic 1 M-row dataset.

Usage: `make bench-optim` (or `uv run python scripts/bench_optim.py [--passes 5 --reps 30]`).

Method (nothing is invented: every figure printed is measured in this run):
1. Synthetic `hcp_population` batch, 10 000 regions x 100 years = 1 000 000 distinct keys, values
   from DuckDB `hash()` with a fixed seed (deterministic, no real data), promoted under
   `gold_ready/`, then the Gold product `population_by_region` is built (1 M rows).
2. A reproducible workload (seeded) goes through the real API (TestClient, journal on): the
   territorial dashboard mix = 60 % `regional_share` pages, 25 % `population_by_region` pages,
   15 % `population_growth` pages. The advisor (config/optimizer.yaml, unchanged) reads that
   journal; the recommendation of the MOST FREQUENT shape is applied with --by/--reason.
3. Measure: `--passes` passes per condition, each in a FRESH Python process, conditions
   interleaved (before, after, before, ...); per pass `--warmup` untimed requests then `--reps`
   timed HTTP requests of the most frequent query (same seeded offsets in both conditions).
   "before" = `ADP_OPTIMIZER_DISABLED=1` (source path), "after" = optimised artefact.
   Same DuckDB thread count (default, printed) in both. OS file cache: warm (not flushed, no
   privilege needed; the warm-up makes both conditions equally warm).
4. Statistic: p95 per pass, then the MEDIAN of the per-pass p95 (spread = standard deviation of
   the per-pass p95). Gain = 1 - median p95 after / median p95 before. Verdict PASS when the
   gain >= 30 % (H-11) and the results are equivalent; the target is never lowered.
"""

import argparse
import json
import os
import random
import secrets
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
TARGET_GAIN = 0.30
REGIONS, YEARS, SEED = 10_000, 100, 42
LIMIT = 50


def generate(store_root: Path) -> None:
    from adp.bronze.store import LocalStore

    store = LocalStore(store_root)
    dest = store_root.parent / "batch.parquet"
    con = duckdb.connect()
    con.execute(
        f"""COPY (
            SELECT 'R' || lpad(r::VARCHAR, 5, '0') AS region, 1900 + y AS year,
                   (1000 + hash(r, y, {SEED}) % 1000000)::BIGINT AS population_total,
                   round((hash(y, r, {SEED}) % 2000) / 100.0, 2) AS unemployment_rate
            FROM range({REGIONS}) a(r), range({YEARS}) b(y) ORDER BY y, r
        ) TO '{dest}' (FORMAT PARQUET)"""
    )
    con.close()
    store.put_if_absent("gold_ready/hcp_population/20240101/data.parquet", dest.read_bytes())
    store.put_if_absent("gold_ready/hcp_population/20240101/manifest.json", b"{}")
    dest.unlink()


def make_client(work: Path, usage_dir: Path | None):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient
    from pydantic import SecretStr
    from sqlalchemy import create_engine

    from adp.api.deps import Deps
    from adp.api.main import create_app
    from adp.bronze.store import LocalStore
    from adp.config import Settings
    from adp.gold.contract import load_products
    from adp.optimizer.usage import UsageConfig, UsageLog
    from adp.quality.quarantine import QuarantineStore
    from adp.quality.score import QualityScoreStore
    from adp.schema.registry import SchemaRegistry, metadata

    engine = create_engine(f"sqlite:///{work / 'meta.db'}")
    metadata.create_all(engine)
    usage = UsageLog(UsageConfig(dir=usage_dir)) if usage_dir else None
    deps = Deps({p.name: p for p in load_products()}, LocalStore(work / "store"),
                SchemaRegistry(engine), QualityScoreStore(engine),
                QuarantineStore(LocalStore(work / "q")), work / "reports", usage=usage)  # fmt: skip
    key = secrets.token_hex(16)  # throw-away key of this local, in-process run
    app = create_app(Settings(_env_file=None, adp_api_keys=SecretStr(key)), deps)  # type: ignore[call-arg]
    return TestClient(app), {"X-API-Key": key}


def offsets(n: int) -> list[int]:
    rnd = random.Random(SEED)
    return [rnd.randrange(0, 200) * LIMIT for _ in range(n)]  # first 200 pages of the dashboard


def workload(work: Path, usage_dir: Path, n: int) -> None:
    rnd = random.Random(SEED)
    client, h = make_client(work, usage_dir)
    with client as c:
        for _ in range(n):
            x = rnd.random()
            off = rnd.randrange(0, 200) * LIMIT
            if x < 0.60:
                url = f"/indicators/regional_share?limit={LIMIT}&offset={off}"
            elif x < 0.85:
                url = f"/products/population_by_region/data?limit={LIMIT}&offset={off}"
            else:
                url = f"/indicators/population_growth?limit={LIMIT}&offset={off}"
            r = c.get(url, headers=h)
            if r.status_code != 200:
                raise SystemExit(f"workload request failed: {r.status_code}")


def worker(args: argparse.Namespace) -> None:
    """One measurement pass in this (fresh) process; prints JSON timings."""
    work = Path(args.work)
    client, h = make_client(work, None)
    offs = offsets(args.warmup + args.reps)
    url = args.url + "?limit={}&offset={}"
    ms = []
    with client as c:
        for i, off in enumerate(offs):
            t = time.perf_counter()
            r = c.get(url.format(LIMIT, off), headers=h)
            dt = (time.perf_counter() - t) * 1000
            if r.status_code != 200:
                raise SystemExit(f"measure request failed: {r.status_code}")
            if i >= args.warmup:
                ms.append(dt)
    threads = duckdb.connect().execute("SELECT current_setting('threads')").fetchone()
    print(json.dumps({"ms": ms, "threads": threads[0] if threads else None}))


def run_pass(work: Path, url: str, cond: str, reps: int, warmup: int) -> dict:  # type: ignore[type-arg]
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    env.pop("ADP_OPTIMIZER_DISABLED", None)
    if cond == "before":
        env["ADP_OPTIMIZER_DISABLED"] = "1"
    out = subprocess.run(
        [sys.executable, __file__, "--worker", "--work", str(work), "--url", url,
         "--reps", str(reps), "--warmup", str(warmup)],
        env=env, cwd=ROOT, check=True, capture_output=True, text=True,
    )  # fmt: skip
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--passes", type=int, default=5)
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--requests", type=int, default=120, help="workload size for the journal")
    ap.add_argument("--output", type=Path, default=ROOT / "docs" / "optimisation.md")
    ap.add_argument("--worker", action="store_true")
    ap.add_argument("--work")
    ap.add_argument("--url")
    args = ap.parse_args()
    if args.worker:
        worker(args)
        return 0
    if args.reps < 30:
        raise SystemExit("--reps doit être >= 30 (méthodologie H-11)")

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(30))
    from adp.bronze.store import LocalStore
    from adp.gold.contract import load_products
    from adp.gold.views import build_product
    from adp.optimizer.advisor import load_advisor_config, recommend
    from adp.optimizer.apply import apply, check_equivalence
    from adp.optimizer.report import Entry, gain, render, stats
    from adp.optimizer.usage import UsageConfig, UsageLog, aggregate

    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="adp-bench-") as tmp:
        work = Path(tmp)
        store = LocalStore(work / "store")
        print(f"[1/6] Jeu synthétique : {REGIONS} régions x {YEARS} années = "
              f"{REGIONS * YEARS:,} lignes (seed {SEED})".replace(",", " "))  # fmt: skip
        generate(work / "store")
        t = time.perf_counter()
        prod = {p.name: p for p in load_products()}["population_by_region"]
        built = build_product(prod, store, store)
        print(f"[2/6] Gold population_by_region : {built.rows:,} lignes en "
              f"{time.perf_counter() - t:.0f} s".replace(",", " "))  # fmt: skip

        usage_dir = work / "usage"
        t = time.perf_counter()
        workload(work, usage_dir, args.requests)
        shapes = aggregate(UsageLog(UsageConfig(dir=usage_dir)).events())
        print(f"[3/6] Charge rejouée via l'API : {args.requests} requêtes en "
              f"{time.perf_counter() - t:.1f} s ; formes (fréquence, p95) :")  # fmt: skip
        for s in shapes:
            print(f"      {s.key} : {s.observations} req, p95 {s.p95:.1f} ms, "
                  f"{s.time_share:.0%} du temps")  # fmt: skip
        top = shapes[0]
        recs = recommend(shapes, load_advisor_config())
        rec = next((r for r in recs if r.shape_key == top.key), None)
        print(f"[4/6] Recommandations : {[r.id for r in recs]}")
        if rec is None:
            print(f"Verdict H-11 : FAIL : aucune recommandation pour la forme la plus fréquente "
                  f"({top.key}) avec les seuils de config/optimizer.yaml")  # fmt: skip
            return 1
        res = apply(store, rec.id, rec.type, rec.target, "bench-optim",
                    "benchmark H-11 (make bench-optim)")  # fmt: skip
        print(f"      appliquée : {rec.id} ({rec.type}) -> {res.base} "
              f"({res.bytes / 1e6:.2f} Mo)")  # fmt: skip

        url = top.route.replace("{name}", top.target)
        print(f"[5/6] Mesure : {url}, {args.passes} passes x {args.reps} répétitions par "
              f"condition (+{args.warmup} d'échauffement), processus isolés, alternés")  # fmt: skip
        runs: dict[str, list[list[float]]] = {"before": [], "after": []}
        threads = set()
        for p in range(args.passes):
            for cond in ("before", "after") if p % 2 == 0 else ("after", "before"):
                r = run_pass(work, url, cond, args.reps, args.warmup)
                runs[cond].append(r["ms"])
                threads.add(r["threads"])
                print(f"      passe {p + 1} {cond:6s} : p50 {stats(r['ms']).p50:7.1f} ms, "
                      f"p95 {stats(r['ms']).p95:7.1f} ms")  # fmt: skip
        p95 = {c: [stats(v).p95 for v in runs[c]] for c in runs}
        med = {c: statistics.median(p95[c]) for c in runs}
        sd = {c: statistics.stdev(p95[c]) if len(p95[c]) > 1 else 0.0 for c in runs}
        pooled = {c: stats([x for v in runs[c] for x in v]) for c in runs}
        g = gain(med["before"], med["after"])
        print("[6/6] Équivalence des résultats (1 M lignes, résultat complet + pages types)...")
        eq = check_equivalence(store, rec.type, rec.target, res.base)
        verdict = "PASS" if g >= TARGET_GAIN and eq else "FAIL"
        print()
        print(f"Threads DuckDB : {sorted(threads)} (identiques dans les deux conditions)")
        for c, label in (("before", "Avant (source)"), ("after", "Après (optimisé)")):
            print(f"{label:17s}: médiane des p95 = {med[c]:.1f} ms (écart-type des p95 "
                  f"{sd[c]:.1f} ms) ; global p50 {pooled[c].p50:.1f} / p95 {pooled[c].p95:.1f} ms, "
                  f"écart-type {pooled[c].stdev:.1f} ms, n = {pooled[c].n}")  # fmt: skip
        print(f"Gain p95 : {g:.1%} (cible H-11 : >= {TARGET_GAIN:.0%}) ; équivalence : "
              f"{'vérifiée' if eq else 'ÉCHEC'}")  # fmt: skip
        print(f"Verdict H-11 : {verdict}")
        entry = Entry(
            rec_id=rec.id, type=rec.type, target=rec.target,
            workload=(f"jeu synthétique 1 000 000 lignes (seed {SEED}) ; charge du tableau de "
                      f"bord : 60 % `regional_share`, 25 % `population_by_region`, 15 % "
                      f"`population_growth`, {args.requests} requêtes ; requête mesurée (la plus "
                      f"fréquente du journal) : `GET {url}`, pages de {LIMIT}, offsets tirés "
                      f"(seed) parmi les 200 premières pages"),
            method=(f"`make bench-optim` : {args.passes} passes par condition, processus "
                    f"isolés alternés, {args.warmup} requêtes d'échauffement puis {args.reps} "
                    f"mesurées par passe, via l'API HTTP (TestClient) ; threads DuckDB "
                    f"{sorted(threads)} ; cache OS chaud (non vidé). Gain = médiane des p95 "
                    f"par passe (avant {med['before']:.1f} ms ± {sd['before']:.1f}, après "
                    f"{med['after']:.1f} ms ± {sd['after']:.1f})"),
            measured_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            before=replace(pooled["before"], p95=med["before"]),
            after=replace(pooled["after"], p95=med["after"]),
            storage_bytes=res.bytes, equivalent=eq, target_gain=TARGET_GAIN,
            notes=(f"Justification de l'advisor : {rec.justification}",
                   "p95 du tableau = médiane des p95 par passe ; p50 et écart-type = toutes "
                   "mesures confondues",
                   f"Durée totale du benchmark : {time.perf_counter() - t0:.0f} s"),
        )  # fmt: skip
        args.output.write_text(render([entry]), encoding="utf-8")
        print(f"Rapport écrit dans {args.output.relative_to(ROOT)} ; durée totale "
              f"{time.perf_counter() - t0:.0f} s")  # fmt: skip
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
