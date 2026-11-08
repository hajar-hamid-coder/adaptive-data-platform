# Scénario de démo : dérive de schéma

Jeux **synthétiques** (aucune donnée réelle) inspirés d'indicateurs socio-économiques par région
marocaine et par année (hypothèse H-01), cohérents avec `config/sources/hcp_population.yaml`
(clé primaire `[region, annee]`). 12 régions x 4 années (2019-2022) = 48 lignes par version.

Chaque dossier `vN/` contient `population.csv` et `expected_changes.json` (opérations attendues
par rapport à la version précédente, lisibles par machine pour P2).

## Diff v1 → v2

| Opération | Colonne | Détail |
|---|---|---|
| `add_column` | `taux_activite` | nouvelle colonne (float) |
| `rename_column` | `population` → `population_totale` | mêmes valeurs |

## Diff v2 → v3

| Opération | Colonne | Détail |
|---|---|---|
| `type_change` | `population_totale` | entier → texte avec espaces (`1 234 567`) |
| `type_change` | `taux_chomage` | float → texte avec virgule décimale et suffixe (`9,3 %`) |
| `drop_column` | `taux_activite` | colonne supprimée |

## Régénérer

```bash
python scripts/drift_simulator.py --seed 42 --out tests/data/drift
```

Sortie déterministe (RNG local `random.Random(seed)`, `\n`, UTF-8, pas d'horodatage) : deux
exécutions avec la même seed donnent des fichiers identiques octet à octet. Les fichiers
commités correspondent à `--seed 42` (vérifié par `tests/test_drift_simulator.py`).

## Lien avec la suite

- P2 : la détection de schéma doit retrouver exactement les opérations de `expected_changes.json`.
- P3 : l'auto-mapping doit proposer `population → population_totale` (règles/LLM) et nettoyer
  les valeurs texte de v3 (types, séparateurs).
- Démo : ingérer v1, puis v2, puis v3 et montrer l'adaptation avec validation humaine
  uniquement sur les cas ambigus.
