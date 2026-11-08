# ADR 0005 — Langages : Python pour le MVP, Rust en option

- Statut : Accepté (hypothèse H-03)
- Date : 2026-10-31

## Contexte

Le schéma d'origine envisage Go, Rust et Python. Le MVP est porté par une seule développeuse ; l'écosystème data (DuckDB, Dagster, pydantic, FastAPI, SDK LLM) est natif en Python. Multiplier les langages ajouterait chaînes de build, CI et interfaces sans gain mesuré.

## Décision

- **Python 3.12** (gestion par `uv`) pour tout le back-end du MVP : ingestion, mapping, qualité, orchestration, API.
- **Rust** en option (tâche P6-08) pour le calcul de similarité de colonnes, seulement si un benchmark montre un goulot ; intégration via PyO3 derrière la même interface Python.
- **Go** non retenu : aucun besoin identifié qu'un service Python ne couvre pas.
- Front-end : TypeScript (React/Vite).

## Conséquences

- Une seule chaîne d'outillage back-end (ruff, mypy, pytest) et une CI simple.
- Les performances CPU restent celles de Python ; les traitements lourds sont délégués à DuckDB (moteur C++).
- L'option Rust n'est déclenchée que sur mesure, ce qui évite une optimisation prématurée.
