# Décisions durables

Format : `YYYY-MM-DD — décision — raison`.

- 2026-10-29 — Source de vérité du backlog = `backlog/backlog.xlsx`, modifié uniquement via `scripts/backlog.py` — évite les conflits d'édition et permet de faire respecter les gates par code.
- 2026-10-29 — Un sous-agent par niveau de modèle (opus/sonnet/haiku) piloté par `/run-phase` — rend le choix du modèle par tâche effectif, pas seulement indicatif.
- 2026-10-29 — « Validé » uniquement posé par `/validate-phase` — l'utilisateur garde la main sur chaque phase.
- 2026-10-29 — MVP 100 % Python, Rust optionnel (H-03) — livrer vite un système mesurable avant d'optimiser.
- 2026-11-08 — Mots de passe de dev par défaut uniformes (`change-me-locally`) dans compose, config et healthcheck — le healthcheck ne lit pas `.env`, les défauts doivent coïncider.
- 2026-11-08 — graphify en mode AST local (`extract --code-only`), `graphify install` non lancé (H-15) — il écrit dans `~/.claude/CLAUDE.md` global.
- 2026-11-08 — Image MinIO `bitnamilegacy/minio` faute d'image officielle publiée (H-14) — à épingler et réévaluer avant passage en public.
