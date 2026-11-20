# Auto-optimisation guidée par l'usage : rapport avant/après

Fichier généré (`adp optimize report` ou `make bench-optim`) : ne pas éditer.
Le texte est déterministe ; seules les mesures (datées) varient d'une exécution à
l'autre. Aucune optimisation n'est appliquée sans `--by` et `--reason` (P6-05).

## `mv-regional_share-0047a114` (materialize_indicator, cible `regional_share`)

- Charge : jeu synthétique 1 000 000 lignes (seed 42) ; charge du tableau de bord : 60 % `regional_share`, 25 % `population_by_region`, 15 % `population_growth`, 120 requêtes ; requête mesurée (la plus fréquente du journal) : `GET /indicators/regional_share`, pages de 50, offsets tirés (seed) parmi les 200 premières pages
- Méthode : `make bench-optim` : 5 passes par condition, processus isolés alternés, 5 requêtes d'échauffement puis 30 mesurées par passe, via l'API HTTP (TestClient) ; threads DuckDB [14] ; cache OS chaud (non vidé). Gain = médiane des p95 par passe (avant 73.3 ms ± 1.8, après 20.2 ms ± 0.5)
- Mesuré le : 2026-11-20 11:25 UTC

| Condition | p50 (ms) | p95 (ms) | écart-type (ms) | n |
|---|---|---|---|---|
| Avant (source) | 70.8 | 73.3 | 1.7 | 150 |
| Après (optimisé) | 18.9 | 20.2 | 0.8 | 150 |

- Gain p95 : **72.4%** ; gain p50 : 73.3%
- Stockage ajouté : 15.26 Mo (artefact additif, `gold_opt/`)
- Équivalence des résultats : vérifiée (résultat complet et pages types identiques)
- Verdict H-11 (réduction p95 ≥ 30%) : **PASS**
- Justification de l'advisor : 66 requêtes observées (≈ 66 pondérées), p95 = 71.8 ms, 69% du temps total des requêtes analytiques
- p95 du tableau = médiane des p95 par passe ; p50 et écart-type = toutes mesures confondues
- Durée totale du benchmark : 196 s
