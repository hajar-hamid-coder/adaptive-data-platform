"""Mapping evaluation on an annotated set (P3-09): rules vs rules+local vs rules+LLM.

Modes
- `rules`: rank-1 candidate of `map_column` (P3-02).
- `rules+local`: cascade of `LLMMapper` with the `LocalProvider` fallback (no key, no network).
- `rules+llm`: same cascade with the real provider (Claude). Without `ANTHROPIC_API_KEY` it is
  `not_measured`: no number is produced, simulated or imputed. With a key, at most
  `--max-llm-columns` unresolved columns are sent (names only, H-09) and the `llm.py` budget
  applies; columns beyond the cap keep their plain `rules` prediction, so that
  `rules+llm` differs from `rules` only where the LLM was consulted.

Two operating points per mode: "proposed" (rank 1 with score >= 0.60, review threshold) and
"auto" (>= 0.90, H-02). A prediction below the point is "(aucune)".

The backlog criterion "precision(rules+llm) >= precision(rules)" is a CHECK that only runs when
the LLM was measured; otherwise it stays "non vérifié" (never silently true).

Exit codes of the CLI: 0 report written (a not-measured LLM prints a visible warning on stderr);
1 LLM measured and criterion violated; 2 `--strict` and LLM not measured (or bad usage/input).
Deterministic: no randomness, no network without key, no timestamp in the report.
"""

import argparse
import csv
import json
import math
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adp.config import Settings
from adp.errors import ConfigError
from adp.mapping.dictionary import SemanticDictionary, load_dictionary
from adp.mapping.llm import (
    AUTO_THRESHOLD,
    REVIEW_THRESHOLD,
    BudgetLedger,
    LLMMapper,
    LLMProvider,
    LocalProvider,
    MappingProposal,
    MappingRequest,
    MappingResponse,
)
from adp.mapping.rules import map_column

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GOLD = ROOT / "tests" / "data" / "gold_mappings.csv"
DEFAULT_DICT = ROOT / "config" / "semantic" / "canonical.yaml"
DEFAULT_OUT = ROOT / "docs" / "eval" / "mapping.md"
MIN_ROWS = 100
DEFAULT_MAX_LLM_COLUMNS = 40
CATEGORIES = ("easy", "noisy", "ambiguous", "unmappable")
SPLITS = ("dev", "holdout")
MODES = ("rules", "rules+local", "rules+llm")
POINTS = {"proposed": REVIEW_THRESHOLD, "auto": AUTO_THRESHOLD}
NONE_LABEL = "(aucune)"
NO_KEY_REASON = "ANTHROPIC_API_KEY absent"


@dataclass(frozen=True)
class GoldRow:
    """One annotated column: names only, never data values."""

    source_col: str
    expected: str | None
    category: str
    split: str
    note: str = ""


@dataclass(frozen=True)
class Prediction:
    """Rank-1 proposal before thresholding."""

    target: str | None
    score: float
    method: str


@dataclass
class LLMRun:
    """Outcome of the rules+llm mode (status `not_measured` carries no predictions)."""

    status: str  # "measured" | "not_measured"
    reason: str = ""
    predictions: dict[str, Prediction] = field(default_factory=dict)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_eur: float = 0.0
    columns_sent: int = 0
    llm_answered: int = 0
    columns_beyond_cap: int = 0
    model: str = ""


# --- gold set ------------------------------------------------------------------------------
def load_gold(
    path: Path, dictionary: SemanticDictionary, *, min_rows: int = MIN_ROWS
) -> list[GoldRow]:
    """Strictly validated annotated set; errors name the file and the line."""
    targets = {f.name for f in dictionary.fields}
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            head = set(reader.fieldnames or [])
            if not {"source_col", "expected_target", "category", "split"} <= head:
                raise ConfigError(f"{path}: missing columns, got {sorted(head)}")
            raw = list(reader)
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    rows: list[GoldRow] = []
    seen: set[str] = set()
    for i, r in enumerate(raw, start=2):
        col, exp = (r["source_col"] or "").strip(), (r["expected_target"] or "").strip()
        cat, split = (r["category"] or "").strip(), (r["split"] or "").strip()
        where = f"{path}:{i}"
        if not col:
            raise ConfigError(f"{where}: empty source_col")
        if col in seen:
            raise ConfigError(f"{where}: duplicate source_col {col!r}")
        seen.add(col)
        if exp and exp not in targets:
            raise ConfigError(f"{where}: unknown target {exp!r}")
        if cat not in CATEGORIES:
            raise ConfigError(f"{where}: invalid category {cat!r}")
        if split not in SPLITS:
            raise ConfigError(f"{where}: invalid split {split!r}")
        if cat == "unmappable" and exp:
            raise ConfigError(f"{where}: unmappable column {col!r} must have no target")
        rows.append(GoldRow(col, exp or None, cat, split, (r.get("note") or "").strip()))
    if len(rows) < min_rows:
        raise ConfigError(f"{path}: {len(rows)} rows, at least {min_rows} required")
    return rows


# --- predictions ---------------------------------------------------------------------------
def predict_rules(columns: Sequence[str], dictionary: SemanticDictionary) -> dict[str, Prediction]:
    """Rank-1 rules candidate per column (no candidate -> target None, score 0)."""
    out: dict[str, Prediction] = {}
    for c in columns:
        cands = map_column(c, dictionary)
        out[c] = (
            Prediction(cands[0].target, cands[0].score, cands[0].method)
            if cands
            else (Prediction(None, 0.0, "-"))
        )
    return out


def _cascade(
    columns: Sequence[str],
    dictionary: SemanticDictionary,
    proposals: Mapping[str, MappingProposal],
) -> dict[str, Prediction]:
    rules = predict_rules(columns, dictionary)
    return {
        c: (Prediction(p.target, p.confidence, p.source) if (p := proposals.get(c)) else rules[c])
        for c in columns
    }


def predict_local(columns: Sequence[str], dictionary: SemanticDictionary) -> dict[str, Prediction]:
    """rules+local: unresolved columns go through the LocalProvider fallback."""
    mapper = LLMMapper(LocalProvider(dictionary), dictionary)
    return _cascade(columns, dictionary, mapper.map(columns))


class _CountingProvider:
    """Counts calls and tokens of the wrapped provider (an attempt counts even if it fails)."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.name, self.model, self.prompt_version = inner.name, inner.model, inner.prompt_version
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def propose(self, request: MappingRequest) -> MappingResponse:
        self.calls += 1
        resp = self.inner.propose(request)
        self.input_tokens += resp.input_tokens
        self.output_tokens += resp.output_tokens
        return resp


def run_llm_mode(
    columns: Sequence[str],
    dictionary: SemanticDictionary,
    provider: LLMProvider,
    *,
    max_columns: int = DEFAULT_MAX_LLM_COLUMNS,
    budget: BudgetLedger | None = None,
) -> LLMRun:
    """rules+llm with `provider`: at most `max_columns` unresolved columns are sent.

    Production callers pass the real Claude provider; tests pass a simulated one to exercise the
    plumbing only. A `LocalProvider` is not an LLM: the run is `not_measured`.
    """
    if isinstance(provider, LocalProvider):
        return LLMRun("not_measured", NO_KEY_REASON)
    counting = _CountingProvider(provider)
    mapper = LLMMapper(counting, dictionary, budget=budget)
    todo = mapper.unresolved(columns)
    sent = todo[: max(0, max_columns)]
    llm = mapper.map(sent) if sent else {}
    answered = sum(1 for p in llm.values() if p.source == "llm")
    if sent and answered == 0:
        return LLMRun(
            "not_measured", "aucune réponse LLM exploitable (repli local, budget ou erreur)",
            calls=counting.calls, columns_sent=len(sent), model=provider.model,
        )  # fmt: skip
    cost = (
        budget.cost(provider.model, counting.input_tokens, counting.output_tokens)
        if budget
        else 0.0
    )
    return LLMRun(
        "measured",
        predictions=_cascade(columns, dictionary, llm),
        calls=counting.calls,
        input_tokens=counting.input_tokens,
        output_tokens=counting.output_tokens,
        cost_eur=cost,
        columns_sent=len(sent),
        llm_answered=answered,
        columns_beyond_cap=len(todo) - len(sent),
        model=provider.model,
    )


# --- metrics -------------------------------------------------------------------------------
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for a proportion k/n (None if n == 0)."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def _at(pred: Prediction, threshold: float) -> str | None:
    return pred.target if pred.target is not None and pred.score >= threshold else None


def _ratio(k: int, n: int) -> float | None:
    return k / n if n else None


def metrics(
    rows: Sequence[GoldRow], preds: Mapping[str, Prediction], threshold: float
) -> dict[str, Any]:
    """Precision/recall/F1/false-positive rate/top-1/coverage at one operating point."""
    got = [(r, _at(preds[r.source_col], threshold)) for r in rows]
    proposed = sum(1 for _, g in got if g is not None)
    correct = sum(1 for r, g in got if r.expected is not None and g == r.expected)
    expected = sum(1 for r, _ in got if r.expected is not None)
    unmappable = [(r, g) for r, g in got if r.category == "unmappable"]
    fp = sum(1 for _, g in unmappable if g is not None)
    prec, rec = _ratio(correct, proposed), _ratio(correct, expected)
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else (0.0 if prec == 0 or rec == 0 else None)
    return {
        "n": len(rows), "n_expected": expected, "n_unmappable": len(unmappable),
        "proposed": proposed, "correct": correct,
        "precision": prec, "precision_ci": wilson(correct, proposed),
        "recall": rec, "recall_ci": wilson(correct, expected),
        "f1": f1, "false_positives": fp, "fp_rate": _ratio(fp, len(unmappable)),
        "top1": _ratio(sum(1 for r, g in got if g == r.expected), len(rows)),
        "coverage": _ratio(proposed, len(rows)),
    }  # fmt: skip


def confusion(
    rows: Sequence[GoldRow], preds: Mapping[str, Prediction], threshold: float
) -> dict[tuple[str, str], int]:
    """(expected, predicted) counts; `(aucune)` is a class on both axes."""
    c: Counter[tuple[str, str]] = Counter()
    for r in rows:
        c[(r.expected or NONE_LABEL, _at(preds[r.source_col], threshold) or NONE_LABEL)] += 1
    return dict(sorted(c.items()))


def errors(
    rows: Sequence[GoldRow], preds: Mapping[str, Prediction], threshold: float
) -> list[dict[str, Any]]:
    """Columns whose thresholded prediction differs from the annotation."""
    out = []
    for r in rows:
        p = preds[r.source_col]
        if _at(p, threshold) != r.expected:
            out.append({
                "source_col": r.source_col, "expected": r.expected or NONE_LABEL,
                "predicted": p.target or NONE_LABEL, "score": p.score, "method": p.method,
                "category": r.category, "split": r.split,
            })  # fmt: skip
    return out


def _subsets(rows: Sequence[GoldRow]) -> dict[str, list[GoldRow]]:
    out = {"all": list(rows)}
    out.update({s: [r for r in rows if r.split == s] for s in SPLITS})
    out.update({f"cat:{c}": [r for r in rows if r.category == c] for c in CATEGORIES})
    return out


def evaluate(
    gold: Sequence[GoldRow], predictions: Mapping[str, Mapping[str, Prediction]]
) -> dict[str, Any]:
    """Metrics per mode, operating point and subset (all, splits, categories) + confusion/errors.

    `predictions[mode][source_col]`; modes absent from `predictions` are simply not evaluated.
    """
    subsets = _subsets(gold)
    result: dict[str, Any] = {}
    for mode, preds in predictions.items():
        result[mode] = {
            point: {
                "metrics": {k: metrics(v, preds, t) for k, v in subsets.items() if v},
                "confusion": confusion(gold, preds, t),
                "errors": errors(gold, preds, t),
            }
            for point, t in POINTS.items()
        }
    return result


def check_criterion(rules_precision: float | None, llm_precision: float | None) -> bool:
    """Backlog criterion: precision(rules+llm) >= precision(rules); None counts as 0."""
    return (llm_precision or 0.0) >= (rules_precision or 0.0)


# --- run + report --------------------------------------------------------------------------
def run_eval(
    gold: Sequence[GoldRow],
    dictionary: SemanticDictionary,
    llm: LLMRun,
) -> dict[str, Any]:
    """Full result: gold summary, per-mode metrics, LLM run info and criterion status."""
    cols = [r.source_col for r in gold]
    preds = {
        "rules": predict_rules(cols, dictionary),
        "rules+local": predict_local(cols, dictionary),
    }
    if llm.status == "measured":
        preds["rules+llm"] = llm.predictions
    res = evaluate(gold, preds)
    criterion: dict[str, Any] = {"status": "not_verified", "reason": llm.reason or NO_KEY_REASON}
    if llm.status == "measured":
        pr = res["rules"]["proposed"]["metrics"]["all"]["precision"]
        pl = res["rules+llm"]["proposed"]["metrics"]["all"]["precision"]
        criterion = {
            "status": "satisfied" if check_criterion(pr, pl) else "violated",
            "precision_rules": pr, "precision_rules_llm": pl,
        }  # fmt: skip
    return {
        "gold": {
            "n": len(gold),
            "category": dict(Counter(r.category for r in gold)),
            "split": dict(Counter(r.split for r in gold)),
        },
        "modes": res,
        "llm": {k: v for k, v in vars(llm).items() if k != "predictions"},
        "criterion": criterion,
    }


def _f(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def _ci(x: tuple[float, float] | None) -> str:
    return "n/a" if x is None else f"[{x[0]:.2f} ; {x[1]:.2f}]"


def _metrics_table(res: Mapping[str, Any], point: str, keys: Sequence[str]) -> list[str]:
    lines = [
        "| Mode | Sous-ensemble | n | Proposées | Précision | IC95 préc. | Rappel | IC95 rappel "
        "| F1 | Faux positifs (unmappable) | Top-1 | Couverture |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for mode, body in res["modes"].items():
        for k in keys:
            m = body[point]["metrics"].get(k)
            if m is None:
                continue
            fp = f"{m['false_positives']}/{m['n_unmappable']}" if m["n_unmappable"] else "-"
            lines.append(
                f"| {mode} | {k} | {m['n']} | {m['proposed']} | {_f(m['precision'])} "
                f"| {_ci(m['precision_ci'])} | {_f(m['recall'])} | {_ci(m['recall_ci'])} "
                f"| {_f(m['f1'])} | {fp} | {_f(m['top1'])} | {_f(m['coverage'])} |"
            )
    return lines


def _confusion_table(conf: Mapping[tuple[str, str], int]) -> list[str]:
    rows = sorted({e for e, _ in conf})
    cols = sorted({p for _, p in conf})
    head = "| attendu \\ prédit | " + " | ".join(f"P{i + 1}" for i in range(len(cols))) + " |"
    lines = [head, "|---|" + "---|" * len(cols)]
    for e in rows:
        cells = [str(conf.get((e, p), "")) or "." for p in cols]
        lines.append(f"| {e} | " + " | ".join(cells) + " |")
    lines += ["", "Colonnes : " + " ; ".join(f"P{i + 1} = {p}" for i, p in enumerate(cols)) + "."]
    return lines


def _names(errs: Sequence[Mapping[str, Any]]) -> str:
    return ", ".join(f"`{e['source_col']}`" for e in errs) or "aucune"


def _errors_table(errs: Sequence[Mapping[str, Any]]) -> list[str]:
    if not errs:
        return ["Aucune erreur."]
    lines = ["| Colonne | Attendu | Prédit | Score | Méthode | Catégorie | Split |",
             "|---|---|---|---|---|---|---|"]  # fmt: skip
    for e in errs:
        col = str(e["source_col"]).replace("|", "\\|")
        lines.append(
            f"| `{col}` | {e['expected']} | {e['predicted']} | {e['score']:.3f} "
            f"| {e['method']} | {e['category']} | {e['split']} |"
        )
    return lines


def render_report(result: Mapping[str, Any]) -> str:
    """French Markdown report from `run_eval`; names and numbers only, deterministic."""
    g, llm, crit = result["gold"], result["llm"], result["criterion"]
    r = result["modes"]["rules"]["proposed"]["metrics"]
    rh, rl = r["holdout"], result["modes"]["rules+local"]["proposed"]["metrics"]["holdout"]
    ra = result["modes"]["rules"]["proposed"]["errors"]
    llm_line = (
        "règles+LLM : NON MESURÉ (ANTHROPIC_API_KEY absent)"
        if llm["status"] != "measured" or llm["reason"]
        else f"règles+LLM : mesuré ({llm['llm_answered']} colonnes répondues par le LLM)"
    )
    if llm["status"] != "measured" and llm["reason"] != NO_KEY_REASON:
        llm_line = f"règles+LLM : NON MESURÉ ({llm['reason']})"
    L = [
        "# Évaluation du mapping (P3-09)",
        "",
        "Rapport généré par `python -m adp.mapping.eval` (ne pas éditer à la main).",
        "",
        "## Résumé",
        "",
        f"- Jeu annoté : {g['n']} colonnes ({g['split'].get('dev', 0)} dev, "
        f"{g['split'].get('holdout', 0)} holdout ; {g['category'].get('unmappable', 0)} sans "
        "correspondance).",
        f"- `rules`, holdout, proposé (>= 0.60) : précision {_f(rh['precision'])}, rappel "
        f"{_f(rh['recall'])}, faux positifs sur unmappable {rh['false_positives']}/"
        f"{rh['n_unmappable']}.",
        f"- `rules+local`, holdout, proposé (>= 0.60) : précision {_f(rl['precision'])}, rappel "
        f"{_f(rl['recall'])}, faux positifs {rl['false_positives']}/{rl['n_unmappable']}.",
        f"- **{llm_line}**.",
        "- Critère du backlog (précision règles+LLM >= précision règles) : "
        + (
            "**NON VÉRIFIÉ** (le LLM n'a pas été mesuré) : ce rapport n'affirme pas qu'il est "
            "satisfait."
            if crit["status"] == "not_verified"
            else f"**{'satisfait' if crit['status'] == 'satisfied' else 'VIOLÉ'}** "
            f"({_f(crit['precision_rules_llm'])} contre {_f(crit['precision_rules'])}, "
            "jeu complet)."
        ),
        "",
        "## Méthodologie et limites",
        "",
        "- Jeu écrit par l'équipe, annotateur unique, **sans double annotation** : les annotations "
        "discutables sont en catégorie `ambiguous` avec une note.",
        "- Split `dev` / `holdout` : le holdout n'a servi qu'à cette mesure, sans réglage des "
        "règles, des seuils ni du dictionnaire. L'annotateur connaissait toutefois le "
        "dictionnaire : les catégories `easy` mesurent surtout la couverture des synonymes.",
        "- Les 30 colonnes bruitées de `tests/mapping/test_rules.py` sont incluses en `dev` "
        "(déjà vues au réglage des règles) : le dev est **optimiste**, le holdout fait foi.",
        "- Jeu de petite taille : les intervalles de confiance (Wilson 95 %) sont larges, ne pas "
        "sur-interpréter une différence de quelques points. Les colonnes `ambiguous` sont "
        "comptées dans la précision et le rappel mais pas dans le taux de faux positifs.",
        "- Deux points d'opération : *proposé* (rang 1 avec score >= 0.60, seuil de revue H-02) "
        "et *auto-appliqué* (score >= 0.90). Sous le seuil la prédiction est « (aucune) ».",
        "- Précision = part correcte parmi les colonnes pour lesquelles une cible est proposée ; "
        "rappel = part retrouvée parmi les colonnes avec cible attendue ; faux positifs = cible "
        "proposée sur une colonne `unmappable`. Seuls des noms de colonnes sont utilisés, "
        "aucune valeur de données.",
        "- `rules+local` : cascade de `LLMMapper` avec le repli local (aucun LLM, aucun réseau). "
        "Il ne donne aucune indication sur la valeur du LLM.",
        "- `rules+llm` n'est mesuré qu'avec une clé API (plafond `--max-llm-columns` et budget de "
        "`llm.py`) ; aucune valeur n'est simulée ni extrapolée.",
        "",
        "## Jeu annoté",
        "",
        "| Catégorie | n |",
        "|---|---|",
        *(f"| {c} | {g['category'].get(c, 0)} |" for c in CATEGORIES),
        "",
        "## Résultats",
        "",
        "### Point « proposé » (score >= 0.60)",
        "",
        *_metrics_table(result, "proposed", ("holdout", "dev", "all")),
        "",
        "Par catégorie (jeu complet) :",
        "",
        *_metrics_table(result, "proposed", tuple(f"cat:{c}" for c in CATEGORIES)),
        "",
        "### Point « auto-appliqué » (score >= 0.90)",
        "",
        *_metrics_table(result, "auto", ("holdout", "dev", "all")),
        "",
        "## Règles + LLM",
        "",
    ]
    if llm["status"] == "measured":
        L += [
            f"- Modèle : {llm['model']} ; colonnes envoyées : {llm['columns_sent']} ; "
            f"réponses LLM : {llm['llm_answered']} ; colonnes non résolues au-delà du plafond "
            f"(prédiction `rules` conservée) : {llm['columns_beyond_cap']}.",
            f"- Appels : {llm['calls']} ; tokens : {llm['input_tokens']} entrée / "
            f"{llm['output_tokens']} sortie ; coût estimé : {llm['cost_eur']:.4f} EUR.",
        ]
    else:
        L += [
            f"**{llm_line}.**",
            "",
            "Le chemin réel est implémenté et testé avec un fournisseur simulé (plomberie "
            "uniquement). Pour le mesurer : exporter `ANTHROPIC_API_KEY` puis relancer la "
            "commande ; le plafond `--max-llm-columns` et le budget mensuel de `llm.py` "
            "s'appliquent. Aucun chiffre n'est affiché ici faute de mesure.",
        ]
    pm = result["modes"]["rules"]["proposed"]
    L += [
        "",
        "## Matrice de confusion (mode `rules`, point proposé, jeu complet)",
        "",
        "Classes présentes uniquement ; `.` = 0 ; `(aucune)` = pas de proposition / pas de cible.",
        "",
        *_confusion_table({k: v for k, v in pm["confusion"].items()}),
        "",
        "### Confusions hors diagonale",
        "",
    ]
    off = [(k, v) for k, v in pm["confusion"].items() if k[0] != k[1]]
    L += [f"- {e} -> {p} : {n}" for (e, p), n in off] or ["- Aucune."]
    L += ["", "## Erreurs détaillées (mode `rules`, point proposé)", "", *_errors_table(ra)]
    local_err = {e["source_col"]: e for e in result["modes"]["rules+local"]["proposed"]["errors"]}
    rules_err = {e["source_col"] for e in ra}
    diff = [e for c, e in local_err.items() if c not in rules_err]
    gone = sorted(rules_err - set(local_err))
    L += [
        "",
        "### Écarts de `rules+local` par rapport à `rules` (point proposé)",
        "",
        f"Erreurs en moins : {len(gone)} ; erreurs en plus : {len(diff)}.",
        "",
        *(_errors_table(diff) if diff else ["Aucune erreur supplémentaire."]),
        "",
        "## Pistes d'amélioration observées",
        "",
        "Pistes déduites des erreurs ci-dessus (non appliquées ici : le holdout ne sert pas à "
        "régler les règles ; correction éventuelle = autre tâche).",
        "",
    ]
    miss = [e for e in ra if e["predicted"] == NONE_LABEL and e["expected"] != NONE_LABEL]
    wrong = [e for e in ra if e["predicted"] != NONE_LABEL and e["expected"] != NONE_LABEL]
    fps = [e for e in ra if e["predicted"] != NONE_LABEL and e["expected"] == NONE_LABEL]
    L += [
        f"- Colonnes attendues sans proposition ({len(miss)}) : {_names(miss)}. Piste : synonymes, "
        "abréviations ou formulations absents du dictionnaire (ou LLM pour les cas non lexicaux).",
        f"- Mauvaise cible proposée ({len(wrong)}) : {_names(wrong)}. Piste : départager par le "
        "type inféré et le profil, ou par une revue humaine.",
        f"- Faux positifs sur colonnes sans correspondance ou ambiguës ({len(fps)}) : "
        f"{_names(fps)}. "
        "Piste : relever le seuil de revue pour le fuzzy ou étendre la liste des jetons "
        "génériques.",
        "",
    ]
    return "\n".join(L)


# --- CLI -----------------------------------------------------------------------------------
def _jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {(" -> ".join(k) if isinstance(k, tuple) else k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(x) for x in o]
    return o


def _llm_run(gold: Sequence[GoldRow], dictionary: SemanticDictionary, max_columns: int) -> LLMRun:
    """Real provider from the environment (never reads `.env`); not measured without a key."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    key = settings.anthropic_api_key
    if key is None or not key.get_secret_value().strip():
        return LLMRun("not_measured", NO_KEY_REASON)
    from adp.mapping.llm import ClaudeProvider  # local import: only used with a key

    budget = BudgetLedger(
        settings.adp_llm_cache_dir / "budget.json", settings.adp_llm_monthly_budget_eur
    )
    provider = ClaudeProvider(key, model=settings.adp_llm_model)
    return run_llm_mode(
        [r.source_col for r in gold], dictionary, provider, max_columns=max_columns, budget=budget
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; see the module docstring for exit codes."""
    ap = argparse.ArgumentParser(prog="python -m adp.mapping.eval", description=__doc__)
    ap.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--json", type=Path, default=None, help="optional metrics JSON")
    ap.add_argument("--max-llm-columns", type=int, default=DEFAULT_MAX_LLM_COLUMNS)
    ap.add_argument("--strict", action="store_true", help="exit 2 if rules+llm is not measured")
    args = ap.parse_args(argv)
    try:
        dictionary = load_dictionary(DEFAULT_DICT)
        gold = load_gold(args.gold, dictionary)
    except ConfigError as exc:
        sys.stderr.write(f"erreur : {exc}\n")
        return 2
    llm = _llm_run(gold, dictionary, args.max_llm_columns)
    result = run_eval(gold, dictionary, llm)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_report(result), encoding="utf-8")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(_jsonable(result), ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    sys.stdout.write(f"rapport : {args.out}\n")
    status = result["criterion"]["status"]
    if status == "violated":
        sys.stderr.write("ECHEC : précision règles+LLM < précision règles.\n")
        return 1
    if status == "not_verified":
        sys.stderr.write(
            f"AVERTISSEMENT : règles+LLM NON MESURÉ ({llm.reason}) ; "
            "critère du backlog NON VÉRIFIÉ.\n"
        )
        return 2 if args.strict else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
