"""Génère des jeux synthétiques v1→v2→v3 avec dérive de schéma (déterministe, stdlib seule).

Usage : python scripts/drift_simulator.py [--seed 42] [--out tests/data/drift] [--years 4]
"""

import argparse
import csv
import json
import random
from pathlib import Path

REGIONS = [
    "Tanger-Tétouan-Al Hoceïma",
    "L'Oriental",
    "Fès-Meknès",
    "Rabat-Salé-Kénitra",
    "Béni Mellal-Khénifra",
    "Casablanca-Settat",
    "Marrakech-Safi",
    "Drâa-Tafilalet",
    "Souss-Massa",
    "Guelmim-Oued Noun",
    "Laâyoune-Sakia El Hamra",
    "Dakhla-Oued Ed-Dahab",
]
FIRST_YEAR = 2019
FILE_NAME = "population.csv"

CHANGES: dict[str, list[dict[str, str]]] = {
    "v1": [],
    "v2": [
        {"op": "add_column", "column": "taux_activite"},
        {"op": "rename_column", "from": "population", "to": "population_totale"},
    ],
    "v3": [
        {"op": "type_change", "column": "population_totale", "from": "integer", "to": "string"},
        {"op": "type_change", "column": "taux_chomage", "from": "float", "to": "string"},
        {"op": "drop_column", "column": "taux_activite"},
    ],
}


def _base(rng: random.Random, years: int) -> list[dict[str, int | float]]:
    """Valeurs de référence (ordre fixe région puis année) partagées par les trois versions."""
    rows: list[dict[str, int | float]] = []
    for region in REGIONS:
        pop = rng.randrange(150_000, 4_500_000)
        for i in range(years):
            pop += rng.randrange(-5_000, 40_000)
            rows.append(
                {
                    "region": region,
                    "annee": FIRST_YEAR + i,
                    "population": pop,
                    "taux_chomage": round(rng.uniform(5.0, 20.0), 1),
                    "taux_activite": round(rng.uniform(38.0, 55.0), 1),
                }
            )
    return rows


def _render(version: str, r: dict) -> dict[str, object]:
    out: dict[str, object] = {"region": r["region"], "annee": r["annee"]}
    if version == "v1":
        out |= {"population": r["population"], "taux_chomage": r["taux_chomage"]}
    elif version == "v2":
        out |= {
            "population_totale": r["population"],
            "taux_chomage": r["taux_chomage"],
            "taux_activite": r["taux_activite"],
        }
    else:  # v3 : texte « 1 234 567 » (espaces) et « 9,3 % » (virgule décimale + suffixe)
        out |= {
            "population_totale": f"{r['population']:,}".replace(",", " "),
            "taux_chomage": f"{r['taux_chomage']:.1f}".replace(".", ",") + " %",
        }
    return out


def generate(seed: int, out: Path, years: int = 4) -> None:
    """Écrit out/v1..v3/{population.csv, expected_changes.json} ; mêmes seed → mêmes octets."""
    base = _base(random.Random(seed), years)
    for version, changes in CHANGES.items():
        folder = out / version
        folder.mkdir(parents=True, exist_ok=True)
        rows = [_render(version, r) for r in base]
        with (folder / FILE_NAME).open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        doc = {"version": version, "file": FILE_NAME, "changes_from_previous": changes}
        text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (folder / "expected_changes.json").write_text(text, encoding="utf-8", newline="")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", type=Path, default=Path("tests/data/drift"))
    p.add_argument("--years", type=int, default=4)
    a = p.parse_args()
    generate(a.seed, a.out, a.years)


if __name__ == "__main__":
    main()
