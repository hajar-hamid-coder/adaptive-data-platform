---
paths:
  - "src/**/*.py"
  - "tests/**/*.py"
  - "scripts/**/*.py"
---

# Règles Python

- Python 3.12, annotations de types complètes ; `from __future__ import annotations` inutile.
- Modèles de données avec pydantic v2 ; erreurs via `adp.errors` (pas d'`Exception` nue).
- Pas d'I/O au niveau module ; fonctions pures pour la logique (diff, scoring, mapping) afin de les tester sans infra.
- Idempotence : rejouer une écriture ne duplique rien (clés/hash explicites).
- Tests : `pytest`, un fichier de test par module, données dans `tests/data/`, graines fixes pour tout aléatoire ; marqueur `slow` pour les tests nécessitant Docker, réseau ou clé LLM.
- Jamais d'`eval`/`exec` sur une donnée externe ou une sortie LLM.
- Logs : `structlog` avec `run_id` ; aucune donnée métier brute dans les logs.
