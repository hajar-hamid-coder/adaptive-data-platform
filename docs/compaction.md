# Compaction et rétention automatisées (P6-06)

Code : `src/adp/optimizer/compaction.py`, `retention.py`, jobs `src/adp/orchestration/maintenance.py`.
Config : `config/compaction.yaml`, `config/retention.yaml` (étendu).

## Portée de la compaction

Bronze et Silver (petits Parquet d'une même partition). **Jamais** Gold (`gold/` immuable adressé
par contenu, `gold_ready/`, `gold_opt/`), ni une racine préfixée `_`. Un fichier n'est candidat que
si : son manifeste existe (lot complet), aucun autre manifeste ne le référence (`bronze_batch`,
`silver_path` : le lignage ne pointe jamais dans un fichier fusionné), sa partition
`ingestion_date=` a plus de `min_age_days`, il est plus petit que `small_bytes`.

## Protocole

Réservation `_compaction/locks/` → schéma identique exigé (sinon refus rapporté) → fichier fusionné
(DuckDB) relu **tel que stocké** → nombre de lignes et hash d'ensemble de lignes (insensible à
l'ordre) égaux aux sources → données, manifeste, puis enregistrement `_compaction/records/<gid>.json`
(point de validation). Aucune suppression ici : les sources restent `superseded`. Dry-run par
défaut ; `--apply` exige `--by` et `--reason`. Interrupteur `ADP_COMPACTION_DISABLED=1`.

Lecteurs : `live_keys()` (à utiliser pour toute union) masque les sources remplacées et les
`compacted-*` sans enregistrement. Un crash laisse donc au pire un doublon invisible, jamais une
perte ; rejouer termine l'opération. La lecture par lot (rejeu) adresse le fichier du lot, inchangée.

## Rétention

Réutilise `lifecycle.py` (archivage Bronze, désormais respectueux de `hold`). Suppressions ajoutées :
fichiers remplacés après `compaction_grace_days` (cible vérifiée par sha256, source non
référencée), artefacts `gold_opt` non servis, lots de quarantaine entièrement résolus. Garde-fous :
dry-run par défaut, plafond `max_deletions` / `max_fraction` (refus de la passe sans
`--confirm-over-cap`), `hold: true` par source, `enabled`, `ADP_RETENTION_DISABLED=1`,
re-planification juste avant suppression, suppression objet par objet, journal append-only
`_retention/journal/` (chemin + règle, écrit AVANT chaque suppression). Les journaux d'audit
(`gold_opt/_journal/`, `_retention/journal/`) ne sont jamais purgés.

## Utilisation

```
adp compact plan | run [--apply --by X --reason Y] [--root DIR] [--report rapport.json]
adp retention plan | run [--apply --by X --reason Y] [--confirm-over-cap] [--root DIR]
```

Jobs Dagster `compaction_job` / `retention_job` : planifications **arrêtées** par défaut, dry-run
tant que `schedule_apply: false`. Métriques : `adp_compaction_{files,bytes}_{before,after}`
(voir `docs/observability.md`).
