"""Benchmarks de performance ADP (P6-09) : `make bench` régénère docs/perf/report.md et results.json.

Usage :
  uv run python scripts/bench.py                       # tous les bancs, écrit docs/perf/*
  uv run python scripts/bench.py --only ingest --rows 100000 --seed 42   # un banc, affiché seul
  uv run python scripts/bench.py --full                # 1 M lignes (plusieurs minutes)

Principes : jeux SYNTHÉTIQUES déterministes (graine fixe, aucune donnée réelle, aucun secret),
échauffement puis répétitions, médiane / écart-type / IQR / p95, chaque mesure accompagnée de la
commande qui la reproduit. Aucun chiffre n'est écrit s'il n'a pas été mesuré : un banc qui échoue
est consigné comme tel. Le banc H-11 (optimisation) RÉUTILISE scripts/bench_optim.py ; la cible
(30 %) vient de ce module et n'est jamais modifiée ici.
"""

import argparse
import gc
import hashlib
import importlib.metadata as md
import json
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
OUT_DIR = ROOT / "docs" / "perf"
DEFAULT_ROWS, FULL_ROWS, DEFAULT_SEED, DEFAULT_REPS = 100_000, 1_000_000, 42, 3
BENCHES = ("ingest", "silver", "gate", "quality", "api", "compaction", "optim")
TITLES = {
    "ingest": "Ingestion Bronze (lecture CSV + écriture Parquet)",
    "silver": "Silver (mapping + nettoyage + dédup + écriture)",
    "gate": "Porte de schéma (profilage + diff)",
    "quality": "Qualité (règles + score)",
    "api": "Latence de l'API (p50 / p95 / p99)",
    "compaction": "Compaction de 200 petits fichiers Parquet",
    "optim": "Gain de l'auto-optimisation (cible H-11)",
}
LIBS = ("duckdb", "pydantic", "fastapi", "starlette", "httpx", "sqlalchemy", "pyyaml", "rapidfuzz")


# --- statistiques (pures) ----------------------------------------------------------------------
def percentile(values: list[float], q: float) -> float:
    """Percentile par rang le plus proche (même définition que l'optimiseur) ; 0.0 si vide."""
    if not values:
        return 0.0
    s = sorted(values)
    rank = -(-q * len(s) // 100)  # plafond
    return s[max(1, int(rank)) - 1]


def stats(values: list[float]) -> dict[str, float]:
    """n, médiane, moyenne, écart-type, IQR (quartiles inclusifs), p95, p99, min, max."""
    n = len(values)
    if n == 0:
        raise ValueError("stats: aucune valeur")
    q = statistics.quantiles(values, n=4, method="inclusive") if n > 1 else [values[0]] * 3
    return {
        "n": n, "median": statistics.median(values), "mean": statistics.fmean(values),
        "stdev": statistics.stdev(values) if n > 1 else 0.0, "iqr": q[2] - q[0],
        "p95": percentile(values, 95), "p99": percentile(values, 99),
        "min": min(values), "max": max(values),
    }  # fmt: skip


def timeit(fn: Callable[[Any], Any], reps: int, warmup: int = 1,
           setup: Callable[[], Any] = lambda: None) -> list[float]:  # fmt: skip
    """Secondes par appel de `fn(setup())` ; `setup` et l'échauffement ne sont pas comptés."""
    out: list[float] = []
    for i in range(warmup + reps):
        arg = setup()
        gc.collect()
        t = time.perf_counter()
        fn(arg)
        dt = time.perf_counter() - t
        if i >= warmup:
            out.append(dt)
    return out


# --- jeu synthétique ---------------------------------------------------------------------------
def gen_rows(rows: int, seed: int) -> list[tuple[str, str, str, str]]:
    """`rows` lignes (region, annee, population, chomage), clé (region, annee) unique sauf 1 %
    de doublons exacts ; 2 % de régions en capitales/espaces (exerce le nettoyage)."""
    rng = random.Random(seed)
    syl = ["ka", "ra", "ba", "ta", "fe", "mo", "sa", "li", "ou", "dar", "ne", "zi", "gha", "ar"]
    k = max(1, min(1000, rows // 100))
    names: list[str] = []
    seen: set[str] = set()
    while len(names) < k:
        n = "".join(rng.choice(syl) for _ in range(rng.randint(3, 5))).capitalize()
        if n not in seen:
            seen.add(n)
            names.append(n)
    out: list[tuple[str, str, str, str]] = []
    for i in range(rows):
        if i % 100 == 99 and out:  # doublon exact de la ligne précédente
            out.append(out[-1])
            continue
        r = names[i % k]
        if i % 50 == 7:
            r = f"  {r.upper()} "
        out.append((r, str(1900 + i // k), str(rng.randrange(1_000, 5_000_000)),
                    f"{rng.randrange(0, 3000) / 100:.2f}"))  # fmt: skip
    return out


def gen_csv(path: Path, rows: int, seed: int) -> int:
    """Écrit le CSV synthétique (octets déterministes) ; retourne sa taille en octets."""
    body = "region,annee,population,chomage\n" + "".join(
        ",".join(r) + "\n" for r in gen_rows(rows, seed)
    )
    path.write_bytes(body.encode("utf-8"))
    return path.stat().st_size


# --- métadonnées -------------------------------------------------------------------------------
def _sh(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=ROOT,
                              check=False).stdout.strip()  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return ""


def hardware() -> dict[str, Any]:
    """Matériel et versions (lectures seules : platform, sysctl, importlib.metadata)."""
    mem = _sh("sysctl", "-n", "hw.memsize")
    if not mem and Path("/proc/meminfo").exists():
        kb = Path("/proc/meminfo").read_text().split()[1]
        mem = str(int(kb) * 1024)
    libs = {}
    for lib in LIBS:
        try:
            libs[lib] = md.version(lib)
        except md.PackageNotFoundError:
            libs[lib] = "absent"
    import duckdb

    threads = duckdb.connect().execute("SELECT current_setting('threads')").fetchone()
    return {
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "cpu": _sh("sysctl", "-n", "machdep.cpu.brand_string") or platform.processor() or "inconnu",
        "cpu_count": os.cpu_count(),
        "ram_gib": round(int(mem) / 2**30, 1) if mem.isdigit() else None,
        "python": platform.python_version(), "duckdb_threads": threads[0] if threads else None,
        "libs": libs,
    }  # fmt: skip


def config_fingerprint() -> str:
    """Empreinte (sha256, 12 hex) du contenu de config/ (noms triés + octets)."""
    h = hashlib.sha256()
    for p in sorted((ROOT / "config").rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(ROOT)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


def commit_id() -> str:
    sha = _sh("git", "rev-parse", "--short=12", "HEAD") or "inconnu"
    dirty = _sh("git", "status", "--porcelain", "--", "src", "scripts", "config")
    return sha + ("+modifié" if dirty else "")


# --- bancs -------------------------------------------------------------------------------------
def _throughput(times: list[float], rows: int, size: int | None = None) -> dict[str, Any]:
    m = statistics.median(times)
    d: dict[str, Any] = {"rows": rows, "times_s": times, "time": stats(times),
                         "rows_per_s": rows / m}  # fmt: skip
    if size is not None:
        d["bytes"], d["mb_per_s"] = size, size / 1e6 / m
    return d


def bench_ingest(p: argparse.Namespace, work: Path) -> dict[str, Any]:
    from adp.bronze.store import LocalStore
    from adp.bronze.writer import write_bronze
    from adp.connectors.files import read_file

    csv = work / "in.csv"
    size = gen_csv(csv, p.rows, p.seed)
    n = [0]

    def run(_: Any) -> None:
        n[0] += 1
        table = read_file(csv)
        write_bronze(table, source="bench", run_id="bench", store=LocalStore(work / f"b{n[0]}"),
                     ingestion_date=date(2024, 1, 1))  # fmt: skip

    d = _throughput(timeit(run, p.reps), p.rows, size)
    shutil.rmtree(work, ignore_errors=True)
    return d


def bench_silver(p: argparse.Namespace, work: Path) -> dict[str, Any]:
    from sqlalchemy import create_engine

    from adp.bronze.store import LocalStore
    from adp.connectors.files import read_file
    from adp.mapping.decision import ReviewQueue
    from adp.mapping.dictionary import load_dictionary
    from adp.mapping.llm import LLMMapper, LocalProvider
    from adp.orchestration.silver_chain import run_silver_chain
    from adp.schema.registry import metadata
    from adp.sources.spec import SourceSpec

    csv = work / "in.csv"
    size = gen_csv(csv, p.rows, p.seed)
    table = read_file(csv)
    d = load_dictionary(ROOT / "config" / "semantic" / "canonical.yaml")
    spec = SourceSpec(name="bench", type="file", uri=str(csv), format="csv",  # type: ignore[arg-type]
                      frequency="manual", primary_key=["region", "annee"])  # fmt: skip
    n = [0]
    last: list[Any] = []

    def setup() -> tuple[Any, Any]:
        n[0] += 1
        eng = create_engine(f"sqlite:///{work / f'q{n[0]}.db'}")
        metadata.create_all(eng)
        return LocalStore(work / f"s{n[0]}"), ReviewQueue(eng, dictionary=d)

    def run(a: tuple[Any, Any]) -> None:
        store, queue = a
        res = run_silver_chain(table, spec=spec, schema_version=1, dictionary=d,
                               mapper=LLMMapper(LocalProvider(d), d), queue=queue,
                               store=store, run_id="bench")  # fmt: skip
        last[:] = [res]

    times = timeit(run, p.reps, setup=setup)
    res = last[0]
    if res.status != "written":
        raise RuntimeError(f"la chaîne Silver n'a pas écrit (statut {res.status})")
    out = _throughput(times, p.rows, size)
    out.update(rows_cleaned=res.rows_cleaned, rows_deduped=res.rows_deduped,
               rows_rejected=res.rows_rejected)  # fmt: skip
    shutil.rmtree(work, ignore_errors=True)
    return out


def bench_gate(p: argparse.Namespace, work: Path) -> dict[str, Any]:
    from adp.connectors.files import RawTable
    from adp.schema.diff import diff_schemas
    from adp.schema.profile import profile_table

    sizes = sorted({max(1000, p.rows // 10), p.rows})
    out: dict[str, Any] = {"sizes": []}
    for n in sizes:
        rows = [list(r) for r in gen_rows(n, p.seed)]
        old = RawTable(["region", "annee", "population", "chomage"], rows)
        # nouveau lot : une colonne renommée, une ajoutée (cas typique de dérive)
        new = RawTable(["region", "annee", "habitants", "chomage", "note"],
                       [[r[0], r[1], r[2], r[3], "x"] for r in rows])  # fmt: skip
        prof = timeit(lambda _, o=old: profile_table(o), p.reps)
        pa, pb = profile_table(old), profile_table(new)
        diff = timeit(lambda _, a=pa, b=pb: diff_schemas(a, b), p.reps)
        out["sizes"].append({
            "rows": n, "profile_s": stats(prof), "diff_s": stats(diff),
            "profile_rows_per_s": n / statistics.median(prof),
            "changes": len(diff_schemas(pa, pb).changes),
        })  # fmt: skip
    return out


def bench_quality(p: argparse.Namespace, work: Path) -> dict[str, Any]:
    from adp.quality.rules import NotNull, Range, Regex, Unique, evaluate
    from adp.quality.score import score_batch

    cols = ["region", "year", "population_total", "unemployment_rate"]
    rows = [[r[0].strip().title(), int(r[1]), int(r[2]), float(r[3])]
            for r in gen_rows(p.rows, p.seed)]  # fmt: skip
    rules = [
        NotNull(id="region_nn", type="not_null", column="region"),
        NotNull(id="year_nn", type="not_null", column="year"),
        Unique(id="key_unique", type="unique", columns=["region", "year"]),
        Range(id="year_ok", type="range", column="year", min=1900, max=2100),
        Range(id="pop_ok", type="range", column="population_total", min=0),
        Range(id="unemp_ok", type="range", column="unemployment_rate", min=0, max=100),
        Regex(id="region_fmt", type="regex", column="region", pattern="^[A-Za-z]+$"),
    ]  # type: ignore[list-item]
    rep = evaluate(rules, cols, rows)
    ev = timeit(lambda _: evaluate(rules, cols, rows), p.reps)
    sc = timeit(lambda _: score_batch(cols, rows, rep, rules), p.reps)
    total = [a + b for a, b in zip(ev, sc, strict=True)]
    return {"rows": p.rows, "n_rules": len(rules), "evaluate_s": stats(ev), "score_s": stats(sc),
            "rows_per_s_evaluate": p.rows / statistics.median(ev),
            "rows_per_s_total": p.rows / statistics.median(total),
            "failed_rows": len(rep.failed_row_indices)}  # fmt: skip


def bench_api(p: argparse.Namespace, work: Path) -> dict[str, Any]:
    import bench_optim as bo

    from adp.bronze.store import LocalStore
    from adp.gold.contract import load_products
    from adp.gold.views import build_product

    bo.REGIONS, bo.YEARS, bo.SEED = max(1, p.rows // 100), 100, p.seed
    store_root = work / "store"
    bo.generate(store_root)
    store = LocalStore(store_root)
    prod = {x.name: x for x in load_products()}["population_by_region"]
    built = build_product(prod, store, store)
    client, headers = bo.make_client(work, None)  # clé éphémère, jamais journalisée
    pages = max(1, min(200, built.rows // bo.LIMIT))
    rnd = random.Random(p.seed)
    offs = [rnd.randrange(0, pages) * bo.LIMIT for _ in range(p.requests + p.warmup)]
    endpoints = {
        "/health": lambda o: "/health",
        "/products/{name}/data": lambda o:
            f"/products/population_by_region/data?limit={bo.LIMIT}&offset={o}",
        "/indicators/{name}": lambda o:
            f"/indicators/regional_share?limit={bo.LIMIT}&offset={o}",
    }  # fmt: skip
    res: dict[str, Any] = {"rows": built.rows, "requests": p.requests, "warmup": p.warmup,
                           "endpoints": {}}  # fmt: skip
    with client as c:
        for name, url in endpoints.items():
            ms: list[float] = []
            for i, o in enumerate(offs):
                t = time.perf_counter()
                r = c.get(url(o), headers=headers)
                dt = (time.perf_counter() - t) * 1000
                if r.status_code != 200:
                    raise RuntimeError(f"{name}: statut HTTP {r.status_code}")
                if i >= p.warmup:
                    ms.append(dt)
            res["endpoints"][name] = {"ms": stats(ms)}
    shutil.rmtree(work, ignore_errors=True)
    return res


def bench_compaction(p: argparse.Namespace, work: Path) -> dict[str, Any]:
    import duckdb

    from adp.bronze.store import LocalStore
    from adp.optimizer.compaction import (
        CompactionConfig,
        compact,
        live_keys,
        plan_compaction,
    )

    files, per = 200, 500
    part = "src/schema_v1/ingestion_date=2024-01-01"
    tpl = work / "tpl"
    tpl.mkdir()
    con = duckdb.connect()
    for i in range(files):
        con.execute(
            f"COPY (SELECT {i * per} + r AS id, 'v' || lpad((hash(r, {i}, {p.seed}) % 100000)"
            f"::VARCHAR, 6, '0') AS v, (hash(r, {i}) % 1000000)::BIGINT AS x "
            f"FROM range({per}) t(r)) TO '{tpl / f'{i:04d}.parquet'}' (FORMAT PARQUET)"
        )
    con.close()
    cfg = CompactionConfig()  # config/compaction.yaml par défaut (8 Mio / 128 Mio)

    def scan(store: LocalStore, keys: list[str]) -> float:
        fs = [str(store.root / k) for k in keys if k.endswith(".parquet")]
        c = duckdb.connect()
        t = time.perf_counter()
        n = c.execute("SELECT count(*), sum(x) FROM read_parquet(?)", [fs]).fetchone()
        dt = time.perf_counter() - t
        c.close()
        assert n and n[0] == files * per
        return dt

    def fresh(k: int) -> LocalStore:
        s = LocalStore(work / f"s{k}")
        for f in sorted(tpl.glob("*.parquet")):
            s.put_if_absent(f"{part}/{f.name}", f.read_bytes())
            s.put_if_absent(f"{part}/{f.stem}.manifest.json", b"{}")
        return s

    times: list[float] = []
    before_scan: list[float] = []
    after_scan: list[float] = []
    rep = None
    for k in range(p.reps + 1):  # k == 0 : échauffement
        s = fresh(k)
        pre = [scan(s, live_keys(s)) for _ in range(5)]
        plan = plan_compaction(s, "", cfg, today=date(2024, 6, 1))
        t = time.perf_counter()
        rep = compact(plan, s, dry_run=False, config=cfg, now=datetime(2024, 6, 1, tzinfo=UTC),
                      by="bench", reason="benchmark P6-09")  # fmt: skip
        dt = time.perf_counter() - t
        post = [scan(s, live_keys(s)) for _ in range(5)]
        if k:
            times.append(dt)
            before_scan.append(statistics.median(pre))
            after_scan.append(statistics.median(post))
    assert rep is not None
    st = next(iter(rep.stats().values()))
    if st["rows_verified"] != files * per or st["files_after"] >= st["files_before"]:
        raise RuntimeError("compaction non vérifiée ou sans effet")
    shutil.rmtree(work, ignore_errors=True)
    return {
        "files_before": st["files_before"], "files_after": st["files_after"],
        "bytes_before": st["bytes_before"], "bytes_after": st["bytes_after"],
        "rows": files * per, "rows_verified": st["rows_verified"],
        "compact_s": stats(times), "scan_before_s": stats(before_scan),
        "scan_after_s": stats(after_scan),
    }  # fmt: skip


def bench_optim_(p: argparse.Namespace, work: Path) -> dict[str, Any]:
    import bench_optim as bo

    out_json = work / "optim.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bench_optim.py"), "--output",
         str(work / "optim.md"), "--json", str(out_json)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if not out_json.exists():
        raise RuntimeError(f"bench_optim sans résultat (code {proc.returncode}) : "
                           f"{proc.stderr.strip()[-200:]}")  # fmt: skip
    d = json.loads(out_json.read_text())
    gain_ok = d["gain_p95"] >= bo.TARGET_GAIN and d["equivalent"]
    d["verdict"] = "PASS" if gain_ok else "FAIL"  # seuil = celui de bench_optim, jamais modifié
    return d


RUNNERS: dict[str, Callable[[argparse.Namespace, Path], dict[str, Any]]] = {
    "ingest": bench_ingest, "silver": bench_silver, "gate": bench_gate,
    "quality": bench_quality, "api": bench_api, "compaction": bench_compaction,
    "optim": bench_optim_,
}  # fmt: skip


def run_bench(name: str, p: argparse.Namespace) -> dict[str, Any]:
    """Exécute un banc ; un échec est consigné SANS aucun chiffre."""
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix=f"adp-bench-{name}-") as tmp:
        try:
            data = RUNNERS[name](p, Path(tmp))
        except Exception as exc:  # noqa: BLE001 - tout échec de banc est rapporté, pas propagé
            return {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "command": command_for(name, p)}  # fmt: skip
    return {"status": "ok", "command": command_for(name, p), "data": data,
            "duration_s": round(time.perf_counter() - t0, 1)}  # fmt: skip


def command_for(name: str, p: argparse.Namespace) -> str:
    """Commande exacte qui rejoue ce banc avec les mêmes paramètres."""
    base = f"uv run python scripts/bench.py --only {name}"
    args = {
        "optim": "", "compaction": f" --seed {p.seed} --reps {p.reps}",
        "api": f" --rows {p.rows} --seed {p.seed} --requests {p.requests} --warmup {p.warmup}",
    }.get(name, f" --rows {p.rows} --seed {p.seed} --reps {p.reps}")  # fmt: skip
    return base + args


# --- rapport (déterministe hors mesures) -------------------------------------------------------
def _f(x: float, nd: int = 1) -> str:
    return f"{x:,.{nd}f}".replace(",", " ")


def _t(rows: list[list[str]], head: list[str]) -> str:
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def _stat_row(label: str, s: dict[str, float], k: float = 1000.0, nd: int = 1) -> list[str]:
    return [label, str(int(s["n"])), _f(s["median"] * k, nd), _f(s["stdev"] * k, nd),
            _f(s["iqr"] * k, nd), _f(s["p95"] * k, nd), _f(s["min"] * k, nd),
            _f(s["max"] * k, nd)]  # fmt: skip


STAT_HEAD = ["Mesure", "n", "médiane (ms)", "écart-type (ms)", "IQR (ms)", "p95 (ms)",
             "min (ms)", "max (ms)"]  # fmt: skip


def section(name: str, r: dict[str, Any] | None) -> str:
    head = f"### {TITLES[name]}\n\n"
    if r is None:
        return head + "Banc non exécuté dans cette génération du rapport.\n"
    cmd = f"Commande de reproduction : `{r['command']}`\n\n"
    if r["status"] != "ok":
        return head + cmd + f"**ÉCHEC du banc : aucun chiffre publié.** Cause : `{r['error']}`\n"
    d, body = r["data"], ""
    if name in ("ingest", "silver"):
        body = _t([_stat_row("temps total", d["time"])], STAT_HEAD)
        body += (f"\nDébit (médiane) : **{_f(d['rows_per_s'], 0)} lignes/s**, "
                 f"**{_f(d['mb_per_s'], 2)} Mo/s** de CSV ({_f(d['bytes'] / 1e6, 2)} Mo, "
                 f"{_f(d['rows'], 0)} lignes).\n")  # fmt: skip
        if name == "silver":
            body += (f"\nLignes propres écrites : {_f(d['rows_cleaned'], 0)} ; doublons retirés : "
                     f"{_f(d['rows_deduped'], 0)} ; rejets : {_f(d['rows_rejected'], 0)}. "
                     f"Le débit couvre mapping, nettoyage, dédup et écriture (une seule mesure "
                     f"bout en bout, sans ventilation).\n")  # fmt: skip
    elif name == "gate":
        rows = []
        for s in d["sizes"]:
            rows.append(_stat_row(f"profilage {_f(s['rows'], 0)} lignes", s["profile_s"]))
            rows.append(_stat_row(f"diff {_f(s['rows'], 0)} lignes", s["diff_s"], nd=3))
        body = _t(rows, STAT_HEAD)
        big = d["sizes"][-1]
        body += (f"\nDébit de profilage (médiane, {_f(big['rows'], 0)} lignes) : "
                 f"**{_f(big['profile_rows_per_s'], 0)} lignes/s**. Le diff compare deux "
                 f"empreintes de colonnes : son coût ne dépend pas du nombre de lignes. "
                 f"Dérive simulée : 1 renommage + 1 ajout ({big['changes']} changements "
                 f"détectés).\n")  # fmt: skip
    elif name == "quality":
        body = _t([_stat_row("évaluation des règles", d["evaluate_s"]),
                   _stat_row("score du lot", d["score_s"])], STAT_HEAD)  # fmt: skip
        body += (f"\n{d['n_rules']} règles sur {_f(d['rows'], 0)} lignes : "
                 f"**{_f(d['rows_per_s_evaluate'], 0)} lignes/s** (évaluation seule), "
                 f"**{_f(d['rows_per_s_total'], 0)} lignes/s** (évaluation + score). "
                 f"{_f(d['failed_rows'], 0)} lignes en échec (doublons injectés).\n")  # fmt: skip
    elif name == "api":
        rows = []
        for ep, v in d["endpoints"].items():
            s = v["ms"]
            rows.append([f"`GET {ep}`", str(int(s["n"])), _f(s["median"], 2), _f(s["p95"], 2),
                         _f(s["p99"], 2), _f(s["stdev"], 2), _f(s["iqr"], 2)])  # fmt: skip
        body = _t(rows, ["Endpoint", "n", "p50 (ms)", "p95 (ms)", "p99 (ms)",
                         "écart-type (ms)", "IQR (ms)"])  # fmt: skip
        body += (f"\nJeu Gold de {_f(d['rows'], 0)} lignes ; {d['requests']} requêtes mesurées "
                 f"par endpoint après {d['warmup']} d'échauffement ; pages de 50, offsets tirés "
                 f"(graine) ; client HTTP en processus (TestClient), sans réseau.\n")  # fmt: skip
    elif name == "compaction":
        body = _t([
            ["fichiers", str(d["files_before"]), str(d["files_after"])],
            ["taille (o)", _f(d["bytes_before"], 0), _f(d["bytes_after"], 0)],
            ["scan `count/sum` (médiane, ms)", _f(d["scan_before_s"]["median"] * 1000, 1),
             _f(d["scan_after_s"]["median"] * 1000, 1)],
        ], ["", "avant", "après"])  # fmt: skip
        sb, sa = d["scan_before_s"]["median"], d["scan_after_s"]["median"]
        body += (f"\nCompaction ({_f(d['rows'], 0)} lignes, {d['rows_verified']} vérifiées par "
                 f"hash) : médiane **{_f(d['compact_s']['median'], 2)} s** (écart-type "
                 f"{_f(d['compact_s']['stdev'], 2)} s, n = {int(d['compact_s']['n'])}). "
                 f"Réduction de taille : {(1 - d['bytes_after'] / d['bytes_before']):.1%} ; "
                 f"scan : x{sb / sa:.1f} plus rapide (scans médians de {int(d['scan_before_s']['n'])}"
                 f" compactions).\n")  # fmt: skip
    elif name == "optim":
        v = d["verdict"]
        body = _t([
            ["avant (source)", _f(d["p95_before_ms"]), _f(d["sd_p95_before_ms"]),
             _f(d["p50_before_ms"])],
            ["après (optimisé)", _f(d["p95_after_ms"]), _f(d["sd_p95_after_ms"]),
             _f(d["p50_after_ms"])],
        ], ["Condition", "médiane des p95 (ms)", "écart-type des p95 (ms)", "p50 global (ms)"])  # fmt: skip
        body += (f"\n{_f(d['rows'], 0)} lignes ; {d['passes']} passes par condition en processus "
                 f"isolés, {d['reps']} requêtes mesurées par passe (+{d['warmup']} d'échauffement) "
                 f"sur `GET {d['url']}` ; threads DuckDB {d['threads']}. Gain p95 : "
                 f"**{d['gain_p95']:.1%}** (cible H-11 : >= {d['target_gain']:.0%}) ; "
                 f"équivalence des résultats : {'vérifiée' if d['equivalent'] else 'ÉCHEC'}. "
                 f"Verdict H-11 : **{v}**. Mesure fournie par `scripts/bench_optim.py` "
                 f"(`make bench-optim`).\n")  # fmt: skip
    return head + cmd + body + f"\nDurée du banc : {r['duration_s']} s.\n"


METHOD = """\
- **Jeux synthétiques** générés par `scripts/bench.py` (graine fixe, mêmes octets pour la même
  graine) : ni donnée réelle ni secret. Ingestion/Silver/qualité : colonnes `region`, `annee`,
  `population`, `chomage` (1 % de doublons exacts, 2 % de régions en capitales avec espaces).
  Compaction : 200 fichiers de 500 lignes. API et optimisation : jeu Gold `population_by_region`.
- **Échauffement** non compté (1 passe pour les débits, `warmup` requêtes pour l'API), puis
  **n répétitions** (n indiqué dans chaque tableau), `gc.collect()` avant chaque répétition.
- **Statistiques** : médiane (valeur de référence), écart-type, IQR (quartiles inclusifs) et p95/p99
  par rang le plus proche ; min/max donnés. Les débits sont calculés sur la médiane des temps.
- **Isolation** : le banc d'optimisation lance chaque passe dans un processus neuf, conditions
  alternées. Les autres bancs tournent dans un même processus (sans isolation).
- **Cache OS chaud** : les fichiers ne sont jamais purgés du cache (pas de privilège requis).
- Les durées couvrent le code applicatif réel (`read_file`, `write_bronze`, `run_silver_chain`,
  `profile_table`, `diff_schemas`, `evaluate`, `score_batch`, API FastAPI, `compact`).
"""

LIMITS = """\
- **Machine unique** (celle décrite ci-dessus) : aucun chiffre n'est transposable à un autre
  matériel ; pas de comparaison avec d'autres systèmes.
- **Jeux synthétiques** : distribution simple, colonnes peu nombreuses, pas de données sales réelles.
- **Charge choisie par nous** : requêtes en pages de 50, pas de concurrence (un client séquentiel),
  latence API mesurée **en processus** (TestClient) : elle exclut réseau, TLS et sérialisation HTTP
  réelle ; elle ne dit rien du débit sous charge parallèle.
- **Cache chaud**, stockage local (`LocalStore`), pas de MinIO/S3 ni de PostgreSQL (SQLite local
  pour les métadonnées) ; le mapping utilise le repli sans LLM (`LocalProvider`), donc aucun appel
  réseau n'est mesuré.
- Compaction : les partitions sont datées de 2024 et la date « du jour » du plan est fixée à
  2024-06-01 pour franchir le garde-fou d'âge ; le gain de scan vaut pour 200 minuscules fichiers
  locaux.
- Variabilité : la machine n'est pas isolée ; l'écart-type et l'IQR donnent l'ordre de grandeur
  du bruit. Une exécution unique ne prouve pas un gain inférieur à ce bruit.
- Le débit Silver n'est PAS linéaire en nombre de lignes (la dédup floue regroupe par blocs de
  texte) : ne pas extrapoler à 1 M de lignes ; le débit Bronze est borné par l'insertion DuckDB
  ligne à ligne du writer actuel, sans optimisation spécifique.
- Le gain H-11 porte sur la requête la plus fréquente de la charge de test, pas sur toutes les
  requêtes.
"""


def render_report(res: dict[str, Any]) -> str:
    """Rapport Markdown à partir d'un résultat JSON (aucune lecture de l'environnement)."""
    m, hw = res["meta"], res["meta"]["hardware"]
    benches = res["benches"]
    opt = benches.get("optim")
    if opt and opt["status"] == "ok":
        d = opt["data"]
        verdict = (
            f"**{d['verdict']}** : réduction du p95 de {d['gain_p95']:.1%} "
            f"(cible H-11 : >= {d['target_gain']:.0%}, jeu de {_f(d['rows'], 0)} lignes)."
        )
    elif opt:
        verdict = "**NON ÉVALUÉ** : le banc d'optimisation a échoué (voir plus bas)."
    else:
        verdict = "**NON ÉVALUÉ** : banc d'optimisation non exécuté."
    libs = ", ".join(f"{k} {v}" for k, v in hw["libs"].items())
    hw_rows = [
        ["Système", hw["os"]], ["Processeur", f"{hw['cpu']} ({hw['cpu_count']} coeurs logiques)"],
        ["Mémoire", f"{hw['ram_gib']} Gio"], ["Python", hw["python"]],
        ["Threads DuckDB", str(hw["duckdb_threads"])], ["Bibliothèques", libs],
    ]  # fmt: skip
    out = [
        "# Rapport de performance\n",
        "Fichier généré par `make bench` (`scripts/bench.py`) : ne pas éditer. Le texte est "
        "déterministe ; seuls les chiffres mesurés (datés) varient d'une exécution à l'autre. "
        "Données brutes : `docs/perf/results.json`.\n",
        f"- Mesuré le : {m['date']}\n- Commit : `{m['commit']}`\n"
        f"- Empreinte de `config/` : `{m['config_fingerprint']}`\n"
        f"- Paramètres : {_f(m['rows'], 0)} lignes, graine {m['seed']}, {m['reps']} répétitions\n"
        f"- Durée totale : {m['total_s']} s\n",
        "## Verdict de la cible H-11\n",
        verdict + "\n",
        "## Matériel et logiciels\n",
        _t(hw_rows, ["Élément", "Valeur"]),
        "## Méthodologie\n",
        METHOD,
        "## Résultats\n",
    ]
    out += [section(n, benches.get(n)) for n in BENCHES]
    out += ["## Limites\n", LIMITS, "## Comment reproduire chaque chiffre\n"]
    out.append(_t([[TITLES[n], f"`{benches[n]['command']}`" if n in benches else "n/a"]
                   for n in BENCHES], ["Banc", "Commande"]))  # fmt: skip
    out.append(
        "`make bench` rejoue l'ensemble et régénère ce rapport et `results.json`. "
        "Les commandes `--only` affichent le résultat sans écrire dans `docs/perf/` "
        "(ajouter `--write` pour mettre à jour les fichiers).\n"
    )
    return "\n".join(out)


def summary(name: str, r: dict[str, Any]) -> str:
    if r["status"] != "ok":
        return f"{name}: ÉCHEC ({r['error']})"
    return section(name, r)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--only", help=f"un ou plusieurs bancs séparés par des virgules : {BENCHES}")
    ap.add_argument("--rows", type=int, default=None, help=f"lignes (défaut {DEFAULT_ROWS})")
    ap.add_argument("--full", action="store_true", help=f"{FULL_ROWS} lignes")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--reps", type=int, default=DEFAULT_REPS)
    ap.add_argument("--requests", type=int, default=300, help="requêtes API mesurées par endpoint")
    ap.add_argument("--warmup", type=int, default=20, help="requêtes API d'échauffement")
    ap.add_argument("--write", action="store_true", help="avec --only : écrit docs/perf/*")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    p = ap.parse_args(argv)
    p.rows = p.rows or (FULL_ROWS if p.full else DEFAULT_ROWS)
    names = list(BENCHES) if not p.only else [n.strip() for n in p.only.split(",")]
    bad = [n for n in names if n not in BENCHES]
    if bad or p.rows < 1000 or p.reps < 1:
        ap.error(f"bancs inconnus {bad} ou --rows < 1000 / --reps < 1")

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(30))
    t0 = time.perf_counter()
    benches: dict[str, Any] = {}
    for n in names:
        print(f"[{n}] ...", flush=True)
        benches[n] = run_bench(n, p)
        print(summary(n, benches[n]), flush=True)
    res = {"meta": {
        "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"), "commit": commit_id(),
        "config_fingerprint": config_fingerprint(), "rows": p.rows, "seed": p.seed,
        "reps": p.reps, "total_s": round(time.perf_counter() - t0), "hardware": hardware(),
    }, "benches": benches}  # fmt: skip
    if not p.only or p.write:
        p.out_dir.mkdir(parents=True, exist_ok=True)
        (p.out_dir / "results.json").write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
        (p.out_dir / "report.md").write_text(render_report(res), encoding="utf-8")
        print(f"Rapport écrit dans {p.out_dir}/report.md ; durée totale {res['meta']['total_s']} s")
    failed = [n for n, r in benches.items() if r["status"] != "ok"]
    o = benches.get("optim")
    h11_fail = bool(o and o["status"] == "ok" and o["data"]["verdict"] != "PASS")
    if failed:
        print(f"Bancs en échec : {failed}")
    if h11_fail:
        print("Verdict H-11 : FAIL")
    return 1 if failed or h11_fail else 0


if __name__ == "__main__":
    sys.exit(main())
