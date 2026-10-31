# ADR 0001 — Architecture médaillon (Bronze / Silver / Gold)

- Statut : Accepté
- Date : 2026-10-31

## Contexte

La plateforme ingère des sources hétérogènes dont le schéma évolue sans préavis. Il faut pouvoir rejouer un traitement après correction d'un mapping, isoler les données invalides et exposer des données fiables, sans perdre l'original. Alternatives écartées : transformation directe source → modèle cible (aucun rejeu possible) ; data vault (trop lourd pour un MVP mono-développeuse).

## Décision

Trois couches immuables et versionnées :

- **Bronze** : copie brute de la source, append-only, avec métadonnées d'ingestion (`run_id`, source, horodatage, empreinte du schéma). Aucune transformation.
- **Silver** : données typées et renommées selon le mapping validé, contrôlées par les règles qualité ; les lignes rejetées vont en **quarantaine** avec leur motif.
- **Gold** : agrégats et modèles métier prêts pour l'API, l'UI et la BI.

Chaque passage de couche est idempotent (clé de partition + empreinte du lot) : rejouer un lot ne duplique rien.

## Conséquences

- Rejeu complet de Silver/Gold depuis Bronze après correction d'un mapping ou d'une règle.
- Coût de stockage accru (données dupliquées entre couches), acceptable sur Parquet compressé.
- La lignée (source → Bronze → Silver → Gold) devient un artefact de premier ordre à tracer dans le metadata store (ADR 0003).
