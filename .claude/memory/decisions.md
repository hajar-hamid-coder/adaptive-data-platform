# Décisions durables

Format : `YYYY-MM-DD — décision — raison`.

- 2026-10-29 — Source de vérité du backlog = `backlog/backlog.xlsx`, modifié uniquement via `scripts/backlog.py` — évite les conflits d'édition et permet de faire respecter les gates par code.
- 2026-10-29 — Un sous-agent par niveau de modèle (opus/sonnet/haiku) piloté par `/run-phase` — rend le choix du modèle par tâche effectif, pas seulement indicatif.
- 2026-10-29 — « Validé » uniquement posé par `/validate-phase` — l'utilisateur garde la main sur chaque phase.
- 2026-10-29 — MVP 100 % Python, Rust optionnel (H-03) — livrer vite un système mesurable avant d'optimiser.
- 2026-11-08 — Mots de passe de dev par défaut uniformes (`change-me-locally`) dans compose, config et healthcheck — le healthcheck ne lit pas `.env`, les défauts doivent coïncider.
- 2026-11-08 — graphify en mode AST local (`extract --code-only`), `graphify install` non lancé (H-15) — il écrit dans `~/.claude/CLAUDE.md` global.
- 2026-11-08 — Image MinIO `bitnamilegacy/minio` faute d'image officielle publiée (H-14) — à épingler et réévaluer avant passage en public.
- 2026-11-08 — Sortie des connecteurs = `RawTable` (colonnes + lignes de chaînes, sans pandas) — Bronze garde le brut, DuckDB écrit le Parquet ; une dépendance de moins.
- 2026-11-08 — Bronze immuable : `put_if_absent` partout, parquet écrit avant le manifeste (manifeste = lot complet), `batch_hash` indépendant du run_id pour détecter le rejeu.
- 2026-11-08 — Archivage = déplacement vers `_archive/` (manifeste déplacé en dernier), jamais de réécriture ; seuil de rejet par lot 20 % (H-02).
- 2026-11-08 — Dagster sans partitions pour le MVP (H-17) ; store `local|s3` choisi par variables d'environnement.
- 2026-11-08 — Diff de schémas : le nom domine le score de renommage (poids 0.7), type et distribution ne font que confirmer ; un changement de FORMAT (format_hint) est distinct d'un changement de TYPE car le profilage relit « 1 080 551 » comme entier.
- 2026-11-08 — Registre : une version = une empreinte structurelle ; un renommage compte comme suppression+ajout donc MAJOR dans `classify_bump` (le diff le détecte à part).
- 2026-11-08 — Schema gate : seul `AUTO_APPLY` laisse passer vers Silver ; `QUEUE_REVIEW` et `BLOCK` arrêtent le lot (Failure Dagster, statut blocked, cause review_required|blocking_change). Le schéma bloqué est quand même enregistré au registre et le Bronze reste intact.
