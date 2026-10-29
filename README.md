# Plateforme de Données Intelligente et Évolutive (ADP)

> De la source à la valeur : une chaîne de traitement intelligente, fiable et évolutive.

Plateforme de données *medallion* (Bronze → Silver → Gold) qui détecte les changements de schéma, propose et applique des mappings (règles + LLM/RAG avec validation humaine), isole les données douteuses en quarantaine, observe ses propres pipelines et s'optimise selon l'usage.

**État : scaffolding terminé, développement piloté par Claude Code phase par phase.** Le README définitif est produit en P7-01.

## Piloter le projet

Le travail est décrit dans [`backlog/backlog.xlsx`](backlog/backlog.xlsx) (8 phases, 64 tâches, ordre d'exécution, modèle par tâche, hypothèses) et résumé dans [`backlog/BACKLOG.md`](backlog/BACKLOG.md).

```bash
git clone <ce dépôt> && cd adaptive-data-platform
claude                      # lance Claude Code dans le dossier (accepter les plugins du projet)
/run-phase P0               # exécute toute la phase P0, puis demande validation
/validate-phase P0          # après relecture : valide, fusionne, annonce la suite
/run-phase P1               # etc.
```

Voir [`CLAUDE.md`](CLAUDE.md) pour le contexte complet donné à Claude, et [`docs/claude-code-setup.md`](docs/claude-code-setup.md) pour le détail du dispositif (hooks, skills, sous-agents, plugins).

## Licence
MIT
