"""Before/after report of an applied optimisation (P6-05), rendered in French Markdown.

Both conditions run the SAME serving function the API uses (`serve_*`), on the same workload
(page size and offsets of the journaled shape), with warm-up, interleaved repetitions and the
optimised path switched by `use_opt`. The text is deterministic apart from the dated figures.
`make bench-optim` (scripts/bench_optim.py) is the rigorous variant (isolated processes, HTTP
path, several passes); this in-process measure is indicative.
"""

import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from adp.bronze.store import ObjectStore
from adp.gold.views import SQL_DIR, latest_gold
from adp.optimizer.apply import serve_indicator, serve_product
from adp.optimizer.usage import percentile


@dataclass(frozen=True)
class Stats:
    n: int
    p50: float
    p95: float
    mean: float
    stdev: float


def stats(ms: Sequence[float]) -> Stats:
    return Stats(len(ms), percentile(ms, 50), percentile(ms, 95), statistics.fmean(ms),
                 statistics.stdev(ms) if len(ms) > 1 else 0.0)  # fmt: skip


def gain(before: float, after: float) -> float:
    """Relative reduction (0.3 = 30 % faster); 0 when `before` is 0."""
    return (before - after) / before if before > 0 else 0.0


@dataclass(frozen=True)
class Entry:
    rec_id: str
    type: str
    target: str
    workload: str
    method: str
    measured_at: str
    before: Stats
    after: Stats
    storage_bytes: int
    equivalent: bool
    target_gain: float | None = None  # H-11 verdict when set
    notes: tuple[str, ...] = ()


def timed(fn: Callable[[], object]) -> float:
    t = time.perf_counter()
    fn()
    return (time.perf_counter() - t) * 1000


def measure(
    store: ObjectStore, rtype: str, target: str, offsets: Sequence[int], limit: int,
    *, reps: int = 30, warmup: int = 3, sql_dir: Path = SQL_DIR,
) -> tuple[list[float], list[float]]:  # fmt: skip
    """Interleaved (ABAB) timings of the source path (A) and the optimised path (B)."""

    def call(use_opt: bool, off: int) -> Callable[[], object]:
        if rtype == "materialize_indicator":
            return lambda: serve_indicator(store, target, limit, off, use_opt=use_opt,
                                           sql_dir=sql_dir)  # fmt: skip
        latest = latest_gold(store, target)
        assert latest is not None
        return lambda: serve_product(store, target, latest[0], latest[1], limit, off,
                                     use_opt=use_opt)  # fmt: skip

    for i in range(warmup):
        for opt in (False, True):
            call(opt, offsets[i % len(offsets)])()
    before, after = [], []
    for i in range(reps):
        off = offsets[i % len(offsets)]
        before.append(timed(call(False, off)))
        after.append(timed(call(True, off)))
    return before, after


def _fmt(s: Stats) -> str:
    return f"{s.p50:.1f} | {s.p95:.1f} | {s.stdev:.1f} | {s.n}"


def render(entries: Sequence[Entry]) -> str:
    out = [
        "# Auto-optimisation guidée par l'usage : rapport avant/après",
        "",
        "Fichier généré (`adp optimize report` ou `make bench-optim`) : ne pas éditer.",
        "Le texte est déterministe ; seules les mesures (datées) varient d'une exécution à",
        "l'autre. Aucune optimisation n'est appliquée sans `--by` et `--reason` (P6-05).",
        "",
    ]
    for e in sorted(entries, key=lambda x: x.rec_id):
        g95, g50 = gain(e.before.p95, e.after.p95), gain(e.before.p50, e.after.p50)
        out += [
            f"## `{e.rec_id}` ({e.type}, cible `{e.target}`)",
            "",
            f"- Charge : {e.workload}",
            f"- Méthode : {e.method}",
            f"- Mesuré le : {e.measured_at}",
            "",
            "| Condition | p50 (ms) | p95 (ms) | écart-type (ms) | n |",
            "|---|---|---|---|---|",
            f"| Avant (source) | {_fmt(e.before)} |",
            f"| Après (optimisé) | {_fmt(e.after)} |",
            "",
            f"- Gain p95 : **{g95:.1%}** ; gain p50 : {g50:.1%}",
            f"- Stockage ajouté : {e.storage_bytes / 1e6:.2f} Mo (artefact additif, `gold_opt/`)",
            "- Équivalence des résultats : "
            + (
                "vérifiée (résultat complet et pages types identiques)"
                if e.equivalent
                else "**ÉCHEC** (résultats différents)"
            ),  # fmt: skip
        ]
        if e.target_gain is not None:
            verdict = "PASS" if g95 >= e.target_gain and e.equivalent else "FAIL"
            out.append(f"- Verdict H-11 (réduction p95 ≥ {e.target_gain:.0%}) : **{verdict}**")
        out += [f"- {n}" for n in e.notes]
        out.append("")
    return "\n".join(out)
