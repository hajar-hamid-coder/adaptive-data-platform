"""Simulateur de dérive : reproductibilité, diff v1/v2/v3, fixtures commitées."""

import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "drift_simulator.py"
COMMITTED = ROOT / "tests" / "data" / "drift"


def run(out: Path, seed: int = 42) -> None:
    subprocess.run(
        [sys.executable, str(SCRIPT), "--seed", str(seed), "--out", str(out)], check=True
    )


def hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def read(root: Path, version: str) -> list[dict[str, str]]:
    with (root / version / "population.csv").open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_same_seed_same_bytes(tmp_path: Path) -> None:
    run(tmp_path / "a")
    run(tmp_path / "b")
    ha = hashes(tmp_path / "a")
    assert len(ha) == 6
    assert ha == hashes(tmp_path / "b")


def test_different_seed_differs(tmp_path: Path) -> None:
    run(tmp_path / "a", 42)
    run(tmp_path / "b", 43)
    assert hashes(tmp_path / "a") != hashes(tmp_path / "b")


def test_v1_to_v2_add_and_rename(tmp_path: Path) -> None:
    run(tmp_path)
    h1, h2 = list(read(tmp_path, "v1")[0]), list(read(tmp_path, "v2")[0])
    assert h1 == ["region", "annee", "population", "taux_chomage"]
    assert h2 == ["region", "annee", "population_totale", "taux_chomage", "taux_activite"]
    r1, r2 = read(tmp_path, "v1"), read(tmp_path, "v2")
    assert [r["population"] for r in r1] == [r["population_totale"] for r in r2]
    ops = json.loads((tmp_path / "v2" / "expected_changes.json").read_text("utf-8"))
    assert [c["op"] for c in ops["changes_from_previous"]] == ["add_column", "rename_column"]


def test_v3_type_change(tmp_path: Path) -> None:
    run(tmp_path)
    rows = read(tmp_path, "v3")
    assert "taux_activite" not in rows[0]
    for r in rows:
        assert not r["taux_chomage"].replace(".", "", 1).isdigit()
        assert r["taux_chomage"].endswith(" %") and "," in r["taux_chomage"]
        assert not r["population_totale"].isdigit()
        assert int(r["population_totale"].replace(" ", "")) > 0


def test_committed_fixtures_match_seed_42(tmp_path: Path) -> None:
    run(tmp_path)
    committed = {k: v for k, v in hashes(COMMITTED).items() if k.startswith("v")}
    assert hashes(tmp_path) == committed
