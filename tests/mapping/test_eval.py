"""Mapping evaluation (P3-09): gold validation, hand-computed metrics, LLM plumbing, report.

The simulated providers below only exercise the comparison machinery: they never produce
published metrics. Regression floors on the real gold file come from the measured run
(docs/eval/mapping.md) with a margin: they detect a future regression, they do not claim a
performance.
"""

import json
from pathlib import Path

import pytest

from adp.errors import ConfigError, LLMError
from adp.mapping import eval as ev
from adp.mapping.dictionary import load_dictionary
from adp.mapping.llm import (
    LocalProvider,
    MappingProposal,
    MappingRequest,
    MappingResponse,
)

ROOT = Path(__file__).resolve().parents[2]
DICT = load_dictionary(ROOT / "config" / "semantic" / "canonical.yaml")
GOLD_PATH = ROOT / "tests" / "data" / "gold_mappings.csv"
HEADER = "source_col,expected_target,category,split,note\n"


def _csv(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "gold.csv"
    p.write_text(HEADER + "\n".join(lines) + "\n", encoding="utf-8")
    return p


# --- load_gold ---------------------------------------------------------------------------
def test_load_gold_valid(tmp_path: Path) -> None:
    p = _csv(tmp_path, ["pop,population_total,easy,dev,", "email,,unmappable,holdout,x"])
    rows = ev.load_gold(p, DICT, min_rows=2)
    assert [(r.source_col, r.expected, r.split) for r in rows] == [
        ("pop", "population_total", "dev"),
        ("email", None, "holdout"),
    ]


@pytest.mark.parametrize(
    ("line", "msg"),
    [
        ("pop,not_a_field,easy,dev,", "unknown target"),
        ("pop,population_total,easy,train,", "invalid split"),
        ("pop,population_total,hard,dev,", "invalid category"),
        ("email,year,unmappable,dev,", "must have no target"),
    ],
)
def test_load_gold_rejects(tmp_path: Path, line: str, msg: str) -> None:
    with pytest.raises(ConfigError, match=msg):
        ev.load_gold(_csv(tmp_path, [line, "x,,unmappable,dev,"]), DICT, min_rows=2)


def test_load_gold_rejects_duplicates_and_small(tmp_path: Path) -> None:
    dup = _csv(tmp_path, ["pop,population_total,easy,dev,", "pop,population_total,easy,dev,"])
    with pytest.raises(ConfigError, match="duplicate"):
        ev.load_gold(dup, DICT, min_rows=2)
    with pytest.raises(ConfigError, match="at least 100"):
        ev.load_gold(_csv(tmp_path, ["pop,population_total,easy,dev,"]), DICT)


# --- Wilson and hand-computed metrics ----------------------------------------------------
def test_wilson() -> None:
    lo, hi = ev.wilson(5, 10) or (0, 0)
    assert lo == pytest.approx(0.2366, abs=1e-3)
    assert hi == pytest.approx(0.7634, abs=1e-3)
    assert ev.wilson(0, 0) is None
    assert ev.wilson(10, 10)[1] == 1.0  # type: ignore[index]


def _mini() -> tuple[list[ev.GoldRow], dict[str, ev.Prediction]]:
    g = [
        ev.GoldRow("a", "year", "easy", "dev"),
        ev.GoldRow("b", "year", "noisy", "dev"),
        ev.GoldRow("c", "region", "noisy", "holdout"),
        ev.GoldRow("d", None, "unmappable", "holdout"),
        ev.GoldRow("e", None, "unmappable", "holdout"),
        ev.GoldRow("f", "region", "ambiguous", "holdout"),
    ]
    p = {
        "a": ev.Prediction("year", 0.95, "synonym"),
        "b": ev.Prediction("region", 0.70, "fuzzy"),
        "c": ev.Prediction(None, 0.0, "-"),
        "d": ev.Prediction("year", 0.65, "fuzzy"),
        "e": ev.Prediction(None, 0.0, "-"),
        "f": ev.Prediction("region", 0.62, "fuzzy"),
    }
    return g, p


def test_metrics_known_values() -> None:
    g, p = _mini()
    m = ev.metrics(g, p, 0.60)
    assert (m["proposed"], m["correct"], m["n_expected"]) == (4, 2, 4)
    assert m["precision"] == 0.5 and m["recall"] == 0.5 and m["f1"] == 0.5
    assert (m["false_positives"], m["n_unmappable"], m["fp_rate"]) == (1, 2, 0.5)
    assert m["top1"] == 0.5 and m["coverage"] == pytest.approx(4 / 6)
    a = ev.metrics(g, p, 0.90)  # only "a" survives
    assert (a["proposed"], a["correct"], a["precision"], a["recall"]) == (1, 1, 1.0, 0.25)
    assert a["f1"] == pytest.approx(0.4) and a["fp_rate"] == 0.0 and a["top1"] == 0.5


def test_confusion_and_errors_known_values() -> None:
    g, p = _mini()
    none = ev.NONE_LABEL
    assert ev.confusion(g, p, 0.60) == {
        ("region", "region"): 1,
        ("region", none): 1,
        ("year", "region"): 1,
        ("year", "year"): 1,
        (none, "year"): 1,
        (none, none): 1,
    }
    assert [e["source_col"] for e in ev.errors(g, p, 0.60)] == ["b", "c", "d"]


def test_evaluate_breakdowns() -> None:
    g, p = _mini()
    res = ev.evaluate(g, {"rules": p})
    ms = res["rules"]["proposed"]["metrics"]
    assert set(ms) >= {"all", "dev", "holdout", "cat:easy", "cat:unmappable"}
    assert ms["dev"]["n"] == 2 and ms["holdout"]["n"] == 4
    assert "rules+llm" not in res


# --- rules+llm plumbing (simulated provider: no published metric) ------------------------
class SimProvider:
    """Answers from `answers` if the target is in the closed candidate list, else abstains."""

    name, model, prompt_version = "sim", "sim-model", "sim-v0"

    def __init__(self, answers: dict[str, str | None], *, wrong: bool = False) -> None:
        self.answers, self.wrong = answers, wrong
        self.calls = 0
        self.seen: list[str] = []

    def propose(self, request: MappingRequest) -> MappingResponse:
        self.calls += 1
        out = []
        for col in request.columns:
            self.seen.append(col.name)
            want = self.answers.get(col.name)
            if self.wrong:  # a deliberately wrong candidate (or abstention if no candidate)
                pool = [c for c in col.candidates if c != want]
                want = pool[0] if pool else None
            target = want if want in col.candidates else None
            out.append(
                MappingProposal(
                    source_col=col.name,
                    target=target,
                    confidence=0.95 if target else 0.0,
                    model=self.model,
                    prompt_version=self.prompt_version,
                    source="llm",
                )  # fmt: skip
            )
        return MappingResponse(
            proposals=tuple(out), input_tokens=100 * len(out), output_tokens=10 * len(out)
        )


@pytest.fixture(scope="module")
def gold() -> list[ev.GoldRow]:
    return ev.load_gold(GOLD_PATH, DICT)


def test_llm_mode_cap_and_counts(gold: list[ev.GoldRow]) -> None:
    cols = [r.source_col for r in gold]
    sim = SimProvider({r.source_col: r.expected for r in gold})
    run = ev.run_llm_mode(cols, DICT, sim, max_columns=25)
    assert run.status == "measured"
    assert run.columns_sent == len(sim.seen) <= 25
    assert run.calls == sim.calls == 2  # 25 columns, batches of 20
    assert run.llm_answered == 25
    assert run.input_tokens == 100 * 25 and run.output_tokens == 10 * 25
    assert run.columns_beyond_cap > 0  # 25 < number of unresolved columns
    assert set(run.predictions) == set(cols)


def test_llm_mode_zero_cap_sends_nothing(gold: list[ev.GoldRow]) -> None:
    sim = SimProvider({})
    run = ev.run_llm_mode([r.source_col for r in gold], DICT, sim, max_columns=0)
    assert sim.calls == 0 and run.columns_sent == 0 and run.status == "measured"


def test_llm_mode_local_provider_not_measured(gold: list[ev.GoldRow]) -> None:
    run = ev.run_llm_mode([r.source_col for r in gold], DICT, LocalProvider(DICT))
    assert run.status == "not_measured" and "ANTHROPIC_API_KEY" in run.reason
    assert run.predictions == {} and run.calls == 0


def test_llm_mode_total_failure_not_measured(gold: list[ev.GoldRow]) -> None:
    class Broken(SimProvider):
        def propose(self, request: MappingRequest) -> MappingResponse:
            self.calls += 1
            raise LLMError("boom")

    run = ev.run_llm_mode([r.source_col for r in gold], DICT, Broken({}), max_columns=40)
    assert run.status == "not_measured" and run.calls >= 1 and run.llm_answered == 0


def test_criterion_check_true_and_false(gold: list[ev.GoldRow]) -> None:
    cols = [r.source_col for r in gold]
    oracle = SimProvider({r.source_col: r.expected for r in gold})
    ok = ev.run_eval(gold, DICT, ev.run_llm_mode(cols, DICT, oracle, max_columns=500))
    assert ok["criterion"]["status"] == "satisfied"
    bad_p = SimProvider({r.source_col: r.expected for r in gold}, wrong=True)
    bad = ev.run_eval(gold, DICT, ev.run_llm_mode(cols, DICT, bad_p, max_columns=500))
    assert bad["criterion"]["status"] == "violated"
    assert "rules+llm" in bad["modes"]
    assert ev.check_criterion(0.9, 0.9) and not ev.check_criterion(0.9, 0.8)
    assert ev.check_criterion(None, None)


def test_unmeasured_run_leaves_criterion_unverified(gold: list[ev.GoldRow]) -> None:
    res = ev.run_eval(gold, DICT, ev.LLMRun("not_measured", ev.NO_KEY_REASON))
    assert res["criterion"]["status"] == "not_verified"
    assert "rules+llm" not in res["modes"]
    report = ev.render_report(res)
    assert "règles+LLM : NON MESURÉ (ANTHROPIC_API_KEY absent)" in report
    assert "NON VÉRIFIÉ" in report and "n'affirme pas" in report


# --- CLI ---------------------------------------------------------------------------------
def test_cli_without_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out, js = tmp_path / "r.md", tmp_path / "r.json"
    assert ev.main(["--out", str(out), "--json", str(js)]) == 0
    assert "NON MESURÉ" in capsys.readouterr().err  # visible warning
    assert "règles+LLM : NON MESURÉ (ANTHROPIC_API_KEY absent)" in out.read_text("utf-8")
    assert json.loads(js.read_text("utf-8"))["criterion"]["status"] == "not_verified"
    assert ev.main(["--out", str(out), "--strict"]) == 2


def test_cli_deterministic_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    assert ev.main(["--out", str(a)]) == 0 and ev.main(["--out", str(b)]) == 0
    assert a.read_bytes() == b.read_bytes()


def test_cli_bad_gold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bad = _csv(tmp_path, ["pop,population_total,easy,dev,"])
    assert ev.main(["--gold", str(bad), "--out", str(tmp_path / "x.md")]) == 2


def test_cli_with_key_uses_cap_and_hides_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gold: list[ev.GoldRow]
) -> None:
    """A key is present: the (simulated) provider is capped; the key never reaches the report."""
    secret = "sk-ant-SENTINEL-KEY-0000"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("ADP_LLM_CACHE_DIR", str(tmp_path / "cache"))
    sim = SimProvider({r.source_col: r.expected for r in gold})

    def fake_claude(key: object, *, model: str) -> SimProvider:
        return sim

    monkeypatch.setattr("adp.mapping.llm.ClaudeProvider", fake_claude)
    out, js = tmp_path / "r.md", tmp_path / "r.json"
    assert ev.main(["--out", str(out), "--json", str(js), "--max-llm-columns", "5"]) == 0
    assert sim.calls == 1 and len(sim.seen) == 5
    data = json.loads(js.read_text("utf-8"))
    assert data["llm"]["status"] == "measured" and data["llm"]["columns_sent"] == 5
    assert data["criterion"]["status"] == "satisfied"
    text = out.read_text("utf-8") + js.read_text("utf-8")
    assert secret not in text and "sk-ant" not in text
    assert "mesuré" in out.read_text("utf-8")


# --- the real gold file ------------------------------------------------------------------
def test_gold_file_shape(gold: list[ev.GoldRow]) -> None:
    assert len(gold) >= 100
    assert sum(r.category == "unmappable" for r in gold) >= 0.20 * len(gold)
    assert sum(r.split == "holdout" for r in gold) >= 30
    assert {r.category for r in gold} == set(ev.CATEGORIES)
    seen_in_tuning = {r.source_col for r in gold if r.note.startswith("vu au réglage")}
    assert all(r.split == "dev" for r in gold if r.source_col in seen_in_tuning)


def test_report_has_no_sentinel_or_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = tmp_path / "r.md"
    ev.main(["--out", str(out)])
    text = out.read_text("utf-8")
    assert "sk-ant" not in text and "SENTINEL" not in text
    committed = ROOT / "docs" / "eval" / "mapping.md"
    if committed.exists():  # the committed report is the real output: same invariants
        assert "sk-ant" not in committed.read_text("utf-8")


def test_regression_floors_on_holdout(gold: list[ev.GoldRow]) -> None:
    """Floors = measured holdout value minus a margin (see docs/eval/mapping.md, 2026-11-12).

    Measured: rules P 0.857 / R 0.857 / FP 4 of 19; rules+local P 0.800 / R 0.914; auto P 1.0.
    """
    cols = [r.source_col for r in gold]
    res = ev.evaluate(
        gold,
        {"rules": ev.predict_rules(cols, DICT), "rules+local": ev.predict_local(cols, DICT)},
    )
    r = res["rules"]["proposed"]["metrics"]["holdout"]
    assert r["precision"] >= 0.857 - 0.05 and r["recall"] >= 0.857 - 0.05
    assert r["fp_rate"] <= 4 / 19 + 0.10
    loc = res["rules+local"]["proposed"]["metrics"]["holdout"]
    assert loc["precision"] >= 0.800 - 0.05 and loc["recall"] >= 0.914 - 0.05
    auto = res["rules"]["auto"]["metrics"]["holdout"]
    assert auto["precision"] >= 1.0 - 0.05 and auto["fp_rate"] == 0.0
