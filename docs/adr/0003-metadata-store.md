# ADR 0003 — Metadata store : PostgreSQL

- Statut : Accepté (hypothèse H-04)
- Date : 2026-10-31

## Contexte

La plateforme doit conserver des métadonnées transactionnelles : sources, versions de schéma, mappings proposés et validés, décisions de revue humaine, résultats qualité, lignée et historique des runs. Ces écritures sont concurrentes (pipelines, API, UI de revue) et doivent être cohérentes. Alternatives écartées : stocker ces métadonnées dans DuckDB (pas d'accès concurrent en écriture) ; SQLite en production (même limite) ; catalogue dédié type OpenMetadata (trop lourd pour le MVP).

## Décision

**PostgreSQL** via Docker Compose comme metadata store, schéma versionné par migrations (couche d'accès précisée en P2-02). **SQLite** est utilisé uniquement dans les tests unitaires pour éviter une dépendance à Docker (H-04).

## Conséquences

- Transactions et contraintes garantissent la cohérence des mappings et des décisions de revue.
- Le SQL doit rester portable entre PostgreSQL et SQLite (pas de type propriétaire dans le cœur) ; les tests d'intégration marqués `slow` tournent sur PostgreSQL réel.
- Couche d'accès (P2-02) : SQLAlchemy Core (tables portables, sans ORM), DDL possédé par Alembic (`alembic.ini`, `migrations/`, URL issue de `Settings`, aucun secret versionné) ; les tables créées à la demande hors registry (`adp_watermarks`) sont ignorées par Alembic. Premier usage : `adp.schema.registry`.
- L'extension `pgvector` pourra servir au RAG du mapping sans nouvelle brique.
