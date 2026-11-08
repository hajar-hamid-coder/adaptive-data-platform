# ADR 0002 — Stockage : Parquet sur MinIO (S3) et moteur DuckDB

- Statut : Accepté
- Date : 2026-10-31

## Contexte

Les trois couches (ADR 0001) doivent être stockées à faible coût, dans un format colonnaire, portable vers un cloud public, et interrogeables en SQL sur un poste local. Alternatives écartées : entrepôt managé (Snowflake, BigQuery : coût et dépendance cloud pour un portfolio) ; Spark (surdimensionné pour les volumes visés) ; Delta Lake / Iceberg dès le MVP (complexité de catalogue prématurée).

## Décision

- Format : **Parquet**, partitionné par source et date d'ingestion.
- Stockage objet : **MinIO** via l'API S3 (Docker Compose), un bucket par couche (`bronze`, `silver`, `gold`, `quarantine`).
- Moteur de calcul : **DuckDB** embarqué (extension `httpfs` pour lire/écrire S3).

## Conséquences

- Code identique en local et sur S3 réel : seule la configuration change.
- DuckDB est mono-processus : pas d'écritures concurrentes sur un même fichier, ce que l'orchestrateur (ADR 0004) doit sérialiser.
- Pas de transactions ACID multi-fichiers : l'idempotence repose sur des écritures dans un chemin temporaire puis renommage, et sur l'empreinte des lots. Iceberg reste une évolution possible si le besoin apparaît.
