# Rapport de performance

Fichier généré par `make bench` (`scripts/bench.py`) : ne pas éditer. Le texte est déterministe ; seuls les chiffres mesurés (datés) varient d'une exécution à l'autre. Données brutes : `docs/perf/results.json`.

- Mesuré le : 2000-01-01 00:00 UTC
- Commit : `abc123def456`
- Empreinte de `config/` : `0123456789ab`
- Paramètres : 100 000 lignes, graine 42, 3 répétitions
- Durée totale : 10 s

## Verdict de la cible H-11

**PASS** : réduction du p95 de 60.0% (cible H-11 : >= 30%, jeu de 1 000 000 lignes).

## Matériel et logiciels

| Élément | Valeur |
|---|---|
| Système | TestOS 1.0 (x86) |
| Processeur | Test CPU (4 coeurs logiques) |
| Mémoire | 8.0 Gio |
| Python | 3.12.0 |
| Threads DuckDB | 4 |
| Bibliothèques | duckdb 1.0, pydantic 2.0 |

## Méthodologie

- **Jeux synthétiques** générés par `scripts/bench.py` (graine fixe, mêmes octets pour la même
  graine) : ni donnée réelle ni secret. Ingestion/Silver/qualité : colonnes `region`, `annee`,
  `population`, `chomage` (1 % de doublons exacts, 2 % de régions en capitales avec espaces).
  Compaction : 200 fichiers de 500 lignes. API et optimisation : jeu Gold `population_by_region`.
- **Échauffement** non compté (1 passe pour les débits, `warmup` requêtes pour l'API), puis
  **n répétitions** (n indiqué dans chaque tableau), `gc.collect()` avant chaque répétition.
- **Statistiques** : médiane (valeur de référence), écart-type, IQR (quartiles inclusifs) et p95/p99
  par rang le plus proche ; min/max donnés. Les débits sont calculés sur la médiane des temps.
- **Isolation** : le banc d'optimisation lance chaque passe dans un processus neuf, conditions
  alternées. Les autres bancs tournent dans un même processus (sans isolation).
- **Cache OS chaud** : les fichiers ne sont jamais purgés du cache (pas de privilège requis).
- Les durées couvrent le code applicatif réel (`read_file`, `write_bronze`, `run_silver_chain`,
  `profile_table`, `diff_schemas`, `evaluate`, `score_batch`, API FastAPI, `compact`).

## Résultats

### Ingestion Bronze (lecture CSV + écriture Parquet)

Commande de reproduction : `uv run python scripts/bench.py --only ingest --rows 100000 --seed 42 --reps 3`

| Mesure | n | médiane (ms) | écart-type (ms) | IQR (ms) | p95 (ms) | min (ms) | max (ms) |
|---|---|---|---|---|---|---|---|
| temps total | 3 | 2 000.0 | 20.0 | 40.0 | 2 200.0 | 1 800.0 | 2 400.0 |

Débit (médiane) : **50 000 lignes/s**, **1.40 Mo/s** de CSV (2.80 Mo, 100 000 lignes).

Durée du banc : 5.0 s.

### Silver (mapping + nettoyage + dédup + écriture)

Commande de reproduction : `uv run python scripts/bench.py --only silver --rows 100000 --seed 42 --reps 3`

| Mesure | n | médiane (ms) | écart-type (ms) | IQR (ms) | p95 (ms) | min (ms) | max (ms) |
|---|---|---|---|---|---|---|---|
| temps total | 3 | 4 000.0 | 40.0 | 80.0 | 4 400.0 | 3 600.0 | 4 800.0 |

Débit (médiane) : **25 000 lignes/s**, **0.70 Mo/s** de CSV (2.80 Mo, 100 000 lignes).

Lignes propres écrites : 99 000 ; doublons retirés : 1 000 ; rejets : 0. Le débit couvre mapping, nettoyage, dédup et écriture (une seule mesure bout en bout, sans ventilation).

Durée du banc : 9.0 s.

### Porte de schéma (profilage + diff)

Commande de reproduction : `uv run python scripts/bench.py --only gate --rows 100000 --seed 42 --reps 3`

| Mesure | n | médiane (ms) | écart-type (ms) | IQR (ms) | p95 (ms) | min (ms) | max (ms) |
|---|---|---|---|---|---|---|---|
| profilage 10 000 lignes | 3 | 50.0 | 0.5 | 1.0 | 55.0 | 45.0 | 60.0 |
| diff 10 000 lignes | 3 | 0.100 | 0.001 | 0.002 | 0.110 | 0.090 | 0.120 |
| profilage 100 000 lignes | 3 | 500.0 | 5.0 | 10.0 | 550.0 | 450.0 | 600.0 |
| diff 100 000 lignes | 3 | 0.100 | 0.001 | 0.002 | 0.110 | 0.090 | 0.120 |

Débit de profilage (médiane, 100 000 lignes) : **200 000 lignes/s**. Le diff compare deux empreintes de colonnes : son coût ne dépend pas du nombre de lignes. Dérive simulée : 1 renommage + 1 ajout (3 changements détectés).

Durée du banc : 1.0 s.

### Qualité (règles + score)

Commande de reproduction : `uv run python scripts/bench.py --only quality --rows 100000 --seed 42 --reps 3`

| Mesure | n | médiane (ms) | écart-type (ms) | IQR (ms) | p95 (ms) | min (ms) | max (ms) |
|---|---|---|---|---|---|---|---|
| évaluation des règles | 3 | 100.0 | 1.0 | 2.0 | 110.0 | 90.0 | 120.0 |
| score du lot | 3 | 10.0 | 0.1 | 0.2 | 11.0 | 9.0 | 12.0 |

7 règles sur 100 000 lignes : **1 000 000 lignes/s** (évaluation seule), **909 090 lignes/s** (évaluation + score). 1 000 lignes en échec (doublons injectés).

Durée du banc : 1.0 s.

### Latence de l'API (p50 / p95 / p99)

Banc non exécuté dans cette génération du rapport.

### Compaction de 200 petits fichiers Parquet

Commande de reproduction : `uv run python scripts/bench.py --only compaction --seed 42 --reps 3`

**ÉCHEC du banc : aucun chiffre publié.** Cause : `RuntimeError: boom`

### Gain de l'auto-optimisation (cible H-11)

Commande de reproduction : `uv run python scripts/bench.py --only optim`

| Condition | médiane des p95 (ms) | écart-type des p95 (ms) | p50 global (ms) |
|---|---|---|---|
| avant (source) | 100.0 | 2.0 | 95.0 |
| après (optimisé) | 40.0 | 1.0 | 38.0 |

1 000 000 lignes ; 5 passes par condition en processus isolés, 30 requêtes mesurées par passe (+5 d'échauffement) sur `GET /indicators/regional_share` ; threads DuckDB [4]. Gain p95 : **60.0%** (cible H-11 : >= 30%) ; équivalence des résultats : vérifiée. Verdict H-11 : **PASS**. Mesure fournie par `scripts/bench_optim.py` (`make bench-optim`).

Durée du banc : 200.0 s.

## Limites

- **Machine unique** (celle décrite ci-dessus) : aucun chiffre n'est transposable à un autre
  matériel ; pas de comparaison avec d'autres systèmes.
- **Jeux synthétiques** : distribution simple, colonnes peu nombreuses, pas de données sales réelles.
- **Charge choisie par nous** : requêtes en pages de 50, pas de concurrence (un client séquentiel),
  latence API mesurée **en processus** (TestClient) : elle exclut réseau, TLS et sérialisation HTTP
  réelle ; elle ne dit rien du débit sous charge parallèle.
- **Cache chaud**, stockage local (`LocalStore`), pas de MinIO/S3 ni de PostgreSQL (SQLite local
  pour les métadonnées) ; le mapping utilise le repli sans LLM (`LocalProvider`), donc aucun appel
  réseau n'est mesuré.
- Compaction : les partitions sont datées de 2024 et la date « du jour » du plan est fixée à
  2024-06-01 pour franchir le garde-fou d'âge ; le gain de scan vaut pour 200 minuscules fichiers
  locaux.
- Variabilité : la machine n'est pas isolée ; l'écart-type et l'IQR donnent l'ordre de grandeur
  du bruit. Une exécution unique ne prouve pas un gain inférieur à ce bruit.
- Le débit Silver n'est PAS linéaire en nombre de lignes (la dédup floue regroupe par blocs de
  texte) : ne pas extrapoler à 1 M de lignes ; le débit Bronze est borné par l'insertion DuckDB
  ligne à ligne du writer actuel, sans optimisation spécifique.
- Le gain H-11 porte sur la requête la plus fréquente de la charge de test, pas sur toutes les
  requêtes.

## Comment reproduire chaque chiffre

| Banc | Commande |
|---|---|
| Ingestion Bronze (lecture CSV + écriture Parquet) | `uv run python scripts/bench.py --only ingest --rows 100000 --seed 42 --reps 3` |
| Silver (mapping + nettoyage + dédup + écriture) | `uv run python scripts/bench.py --only silver --rows 100000 --seed 42 --reps 3` |
| Porte de schéma (profilage + diff) | `uv run python scripts/bench.py --only gate --rows 100000 --seed 42 --reps 3` |
| Qualité (règles + score) | `uv run python scripts/bench.py --only quality --rows 100000 --seed 42 --reps 3` |
| Latence de l'API (p50 / p95 / p99) | n/a |
| Compaction de 200 petits fichiers Parquet | `uv run python scripts/bench.py --only compaction --seed 42 --reps 3` |
| Gain de l'auto-optimisation (cible H-11) | `uv run python scripts/bench.py --only optim` |

`make bench` rejoue l'ensemble et régénère ce rapport et `results.json`. Les commandes `--only` affichent le résultat sans écrire dans `docs/perf/` (ajouter `--write` pour mettre à jour les fichiers).
