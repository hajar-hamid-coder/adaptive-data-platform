# Rapport de performance

Fichier généré par `make bench` (`scripts/bench.py`) : ne pas éditer. Le texte est déterministe ; seuls les chiffres mesurés (datés) varient d'une exécution à l'autre. Données brutes : `docs/perf/results.json`.

- Mesuré le : 2026-11-20 12:06 UTC
- Commit : `f6eb8c06c66a+modifié`
- Empreinte de `config/` : `8208ca5bf77b`
- Paramètres : 100 000 lignes, graine 42, 3 répétitions
- Durée totale : 448 s

## Verdict de la cible H-11

**PASS** : réduction du p95 de 73.8% (cible H-11 : >= 30%, jeu de 1 000 000 lignes).

## Matériel et logiciels

| Élément | Valeur |
|---|---|
| Système | Darwin 25.5.0 (arm64) |
| Processeur | Apple M3 Max (14 coeurs logiques) |
| Mémoire | 36.0 Gio |
| Python | 3.12.13 |
| Threads DuckDB | 14 |
| Bibliothèques | duckdb 2.0.0, pydantic 2.14.1, fastapi 0.165.0, starlette 1.10.0, httpx 0.28.1, sqlalchemy 2.1.8, pyyaml 6.0.3, rapidfuzz 3.14.6 |

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
| temps total | 3 | 11 992.1 | 43.7 | 42.7 | 12 018.9 | 11 933.4 | 12 018.9 |

Débit (médiane) : **8 339 lignes/s**, **0.23 Mo/s** de CSV (2.81 Mo, 100 000 lignes).

Durée du banc : 48.6 s.

### Silver (mapping + nettoyage + dédup + écriture)

Commande de reproduction : `uv run python scripts/bench.py --only silver --rows 100000 --seed 42 --reps 3`

| Mesure | n | médiane (ms) | écart-type (ms) | IQR (ms) | p95 (ms) | min (ms) | max (ms) |
|---|---|---|---|---|---|---|---|
| temps total | 3 | 39 945.4 | 173.3 | 161.8 | 40 214.7 | 39 891.1 | 40 214.7 |

Débit (médiane) : **2 503 lignes/s**, **0.07 Mo/s** de CSV (2.81 Mo, 100 000 lignes).

Lignes propres écrites : 100 000 ; doublons retirés : 2 900 ; rejets : 0. Le débit couvre mapping, nettoyage, dédup et écriture (une seule mesure bout en bout, sans ventilation).

Durée du banc : 162.2 s.

### Porte de schéma (profilage + diff)

Commande de reproduction : `uv run python scripts/bench.py --only gate --rows 100000 --seed 42 --reps 3`

| Mesure | n | médiane (ms) | écart-type (ms) | IQR (ms) | p95 (ms) | min (ms) | max (ms) |
|---|---|---|---|---|---|---|---|
| profilage 10 000 lignes | 3 | 35.5 | 0.1 | 0.1 | 35.6 | 35.4 | 35.6 |
| diff 10 000 lignes | 3 | 0.097 | 0.009 | 0.009 | 0.110 | 0.092 | 0.110 |
| profilage 100 000 lignes | 3 | 378.8 | 3.7 | 3.5 | 380.1 | 373.2 | 380.1 |
| diff 100 000 lignes | 3 | 0.097 | 0.008 | 0.008 | 0.106 | 0.091 | 0.106 |

Débit de profilage (médiane, 100 000 lignes) : **263 992 lignes/s**. Le diff compare deux empreintes de colonnes : son coût ne dépend pas du nombre de lignes. Dérive simulée : 1 renommage + 1 ajout (3 changements détectés).

Durée du banc : 3.3 s.

### Qualité (règles + score)

Commande de reproduction : `uv run python scripts/bench.py --only quality --rows 100000 --seed 42 --reps 3`

| Mesure | n | médiane (ms) | écart-type (ms) | IQR (ms) | p95 (ms) | min (ms) | max (ms) |
|---|---|---|---|---|---|---|---|
| évaluation des règles | 3 | 110.1 | 4.0 | 3.6 | 116.6 | 109.4 | 116.6 |
| score du lot | 3 | 13.8 | 0.1 | 0.1 | 14.0 | 13.7 | 14.0 |

7 règles sur 100 000 lignes : **908 367 lignes/s** (évaluation seule), **806 190 lignes/s** (évaluation + score). 1 000 lignes en échec (doublons injectés).

Durée du banc : 1.0 s.

### Latence de l'API (p50 / p95 / p99)

Commande de reproduction : `uv run python scripts/bench.py --only api --rows 100000 --seed 42 --requests 300 --warmup 20`

| Endpoint | n | p50 (ms) | p95 (ms) | p99 (ms) | écart-type (ms) | IQR (ms) |
|---|---|---|---|---|---|---|
| `GET /health` | 300 | 0.86 | 0.93 | 0.95 | 0.03 | 0.03 |
| `GET /products/{name}/data` | 300 | 11.56 | 11.86 | 12.53 | 0.24 | 0.19 |
| `GET /indicators/{name}` | 300 | 42.57 | 44.09 | 45.61 | 0.79 | 0.96 |

Jeu Gold de 100 000 lignes ; 300 requêtes mesurées par endpoint après 20 d'échauffement ; pages de 50, offsets tirés (graine) ; client HTTP en processus (TestClient), sans réseau.

Durée du banc : 32.3 s.

### Compaction de 200 petits fichiers Parquet

Commande de reproduction : `uv run python scripts/bench.py --only compaction --seed 42 --reps 3`

|  | avant | après |
|---|---|---|
| fichiers | 200 | 1 |
| taille (o) | 1 600 947 | 1 444 425 |
| scan `count/sum` (médiane, ms) | 11.5 | 1.0 |

Compaction (100 000 lignes, 100000 vérifiées par hash) : médiane **0.19 s** (écart-type 0.00 s, n = 3). Réduction de taille : 9.8% ; scan : x11.7 plus rapide (scans médians de 3 compactions).

Durée du banc : 3.2 s.

### Gain de l'auto-optimisation (cible H-11)

Commande de reproduction : `uv run python scripts/bench.py --only optim`

| Condition | médiane des p95 (ms) | écart-type des p95 (ms) | p50 global (ms) |
|---|---|---|---|
| avant (source) | 71.7 | 2.8 | 69.8 |
| après (optimisé) | 18.7 | 0.4 | 18.1 |

1 000 000 lignes ; 5 passes par condition en processus isolés, 30 requêtes mesurées par passe (+5 d'échauffement) sur `GET /indicators/regional_share` ; threads DuckDB [14]. Gain p95 : **73.8%** (cible H-11 : >= 30%) ; équivalence des résultats : vérifiée. Verdict H-11 : **PASS**. Mesure fournie par `scripts/bench_optim.py` (`make bench-optim`).

Durée du banc : 197.6 s.

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
| Latence de l'API (p50 / p95 / p99) | `uv run python scripts/bench.py --only api --rows 100000 --seed 42 --requests 300 --warmup 20` |
| Compaction de 200 petits fichiers Parquet | `uv run python scripts/bench.py --only compaction --seed 42 --reps 3` |
| Gain de l'auto-optimisation (cible H-11) | `uv run python scripts/bench.py --only optim` |

`make bench` rejoue l'ensemble et régénère ce rapport et `results.json`. Les commandes `--only` affichent le résultat sans écrire dans `docs/perf/` (ajouter `--write` pour mettre à jour les fichiers).
