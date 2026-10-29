# Décisions durables

Format : `YYYY-MM-DD — décision — raison`.

- 2026-10-29 — Source de vérité du backlog = `backlog/backlog.xlsx`, modifié uniquement via `scripts/backlog.py` — évite les conflits d'édition et permet de faire respecter les gates par code.
- 2026-10-29 — Un sous-agent par niveau de modèle (opus/sonnet/haiku) piloté par `/run-phase` — rend le choix du modèle par tâche effectif, pas seulement indicatif.
- 2026-10-29 — « Validé » uniquement posé par `/validate-phase` — l'utilisateur garde la main sur chaque phase.
- 2026-10-29 — MVP 100 % Python, Rust optionnel (H-03) — livrer vite un système mesurable avant d'optimiser.
