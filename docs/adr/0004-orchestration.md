# ADR 0004 — Orchestration : Dagster

- Statut : Accepté (hypothèse H-08)
- Date : 2026-10-31

## Contexte

Il faut planifier et rejouer les pipelines source → Bronze → Silver → Gold, suivre la fraîcheur de chaque table et déclencher un traitement lors de l'arrivée d'un fichier ou d'un changement de schéma. Alternatives écartées : Airflow (orienté tâches plutôt que données, plus lourd à exécuter en local) ; Prefect (modèle d'assets moins abouti) ; cron + scripts (ni lignée, ni rejeu, ni UI).

## Décision

**Dagster**, avec un *asset* par table et par couche, des *sensors* pour les arrivées de fichiers et les changements de schéma, et des *asset checks* pour la qualité.

## Conséquences

- Le graphe d'assets reflète directement le médaillon (ADR 0001) et fournit lignée et UI de rejeu.
- Les écritures concurrentes sur un même fichier DuckDB sont évitées par la configuration de concurrence des runs (ADR 0002).
- La logique métier (détection, mapping, qualité) reste dans des fonctions pures du paquet `adp`, Dagster n'étant qu'une couche d'appel : changer d'orchestrateur resterait possible.
