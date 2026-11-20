"""P6-09 : mécanique du banc (statistiques, jeu synthétique, rendu du rapport), petits N."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import structlog

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("bench", ROOT / "scripts" / "bench.py")
assert _spec and _spec.loader
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


@pytest.fixture(autouse=True)
def _restore_structlog():  # type: ignore[no-untyped-def]
    """`bench.main` reconfigure structlog (globalement) : le restaurer pour les autres tests."""
    cfg = structlog.get_config()
    yield
    structlog.configure(**cfg)


FIXTURE = json.loads((HERE / "results_fixture.json").read_text(encoding="utf-8"))


def test_stats_on_known_values() -> None:
    s = bench.stats([float(i) for i in range(1, 21)])
    assert s["n"] == 20
    assert s["median"] == 10.5
    assert s["p95"] == 19.0  # rang le plus proche : ceil(0,95 x 20) = 19e valeur
    assert s["p99"] == 20.0
    assert s["iqr"] == pytest.approx(9.5)  # quartiles inclusifs 5,75 et 15,25
    assert (s["min"], s["max"]) == (1.0, 20.0)
    assert s["stdev"] == pytest.approx(5.916079783)


def test_stats_edge_cases() -> None:
    one = bench.stats([3.0])
    assert one["median"] == one["p95"] == 3.0 and one["stdev"] == 0.0 and one["iqr"] == 0.0
    assert bench.percentile([], 95) == 0.0
    with pytest.raises(ValueError):
        bench.stats([])


def test_timeit_excludes_warmup_and_setup() -> None:
    calls: list[int] = []
    out = bench.timeit(lambda a: calls.append(a), reps=3, warmup=2, setup=lambda: len(calls))
    assert len(out) == 3 and len(calls) == 5


def test_synthetic_data_is_deterministic(tmp_path: Path) -> None:
    a, b, c = (tmp_path / n for n in "abc")
    bench.gen_csv(a, 2000, 42)
    bench.gen_csv(b, 2000, 42)
    bench.gen_csv(c, 2000, 43)
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    assert sha(a) == sha(b)
    assert sha(a) != sha(c)
    lines = a.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "region,annee,population,chomage" and len(lines) == 2001


def test_report_matches_golden() -> None:
    expected = (HERE / "report_golden.md").read_text(encoding="utf-8")
    assert bench.render_report(FIXTURE) == expected


def test_every_section_has_its_reproduction_command() -> None:
    report = bench.render_report(FIXTURE)
    for name, r in FIXTURE["benches"].items():
        sec = bench.section(name, r)
        assert r["command"] in sec  # dans la section du banc
        assert r["command"] in report.split("## Comment reproduire chaque chiffre")[1]
    assert "non exécuté" in bench.section("api", None)


def test_failed_bench_writes_no_figure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(p: object, work: Path) -> dict[str, object]:
        raise RuntimeError("boom")

    monkeypatch.setitem(bench.RUNNERS, "ingest", boom)
    p = bench.argparse.Namespace(rows=1000, seed=1, reps=1, requests=1, warmup=0)
    r = bench.run_bench("ingest", p)
    assert r["status"] == "failed" and "data" not in r and "boom" in r["error"]
    text = bench.section("ingest", r)
    assert "ÉCHEC" in text and "lignes/s" not in text and "médiane" not in text
    sec = bench.section("compaction", FIXTURE["benches"]["compaction"])
    assert "ÉCHEC" in sec and "RuntimeError: boom" in sec and "|" not in sec


def test_h11_verdict_is_explicit_and_target_not_edited() -> None:
    import bench_optim

    assert bench_optim.TARGET_GAIN == 0.30
    assert "**PASS**" in bench.render_report(FIXTURE)
    bad = json.loads(json.dumps(FIXTURE))
    bad["benches"]["optim"]["data"].update(gain_p95=0.2, verdict="FAIL")
    assert "**FAIL**" in bench.render_report(bad)
    bad["benches"]["optim"] = {"status": "failed", "error": "x", "command": "c"}
    assert "NON ÉVALUÉ" in bench.render_report(bad)


def test_end_to_end_small(tmp_path: Path) -> None:
    out = tmp_path / "perf"
    argv = ["--only", "ingest,gate,quality,silver", "--rows", "1000", "--reps", "1",
            "--write", "--out-dir", str(out)]  # fmt: skip
    assert bench.main(argv) == 0
    res = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert all(r["status"] == "ok" for r in res["benches"].values())
    assert res["meta"]["hardware"]["python"] and res["meta"]["config_fingerprint"]
    report = (out / "report.md").read_text(encoding="utf-8")
    assert all(r["command"] in report for r in res["benches"].values())
    assert bench.main(["--only", "gate", "--rows", "1000", "--reps", "1",
                       "--out-dir", str(tmp_path / "none")]) == 0  # fmt: skip
    assert not (tmp_path / "none").exists()  # --only seul n'écrit rien
