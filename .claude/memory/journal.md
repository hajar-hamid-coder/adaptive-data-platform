# Journal

- 2026-10-29 — Scaffolding Claude Code créé : backlog Excel (64 tâches, 8 phases), hooks, skills, sous-agents, mémoire. Aucune tâche démarrée.
- 2026-10-29 — P0-01 repo + outillage uv/ruff/mypy/pytest (aeae3df). Python épinglé 3.12 (.python-version) ; scripts/ exclu de ruff.
- 2026-10-31 — P0-02 Docker Compose MinIO+Postgres (f8aba05). Postgres sur 5433 ; image bitnamilegacy/minio (minio/minio retirée de Docker Hub).
- 2026-10-31 — P0-03 Settings pydantic-settings + hook check-secrets (1f8ef18).
- 2026-10-31 — P0-04 logging structlog + erreurs domaine (ffe5437).
- 2026-10-31 — P0-05 CI GitHub Actions (2c0d950). Run Actions réel à confirmer après push.
- 2026-10-31 — P0-06 graphify via uv tool, mode AST local (d81f9d6). Rapport à régénérer à la validation.
- 2026-10-31 — P0-07 6 ADR + architecture.md Mermaid (3620cb1).
- 2026-11-08 — Bilan P0 : 7/7 tâches validées, 19 tests verts, 6 ADR, graphe de 14 communautés. Ouvert : H-14 image MinIO, H-15 graphify install, run CI GitHub à confirmer.
- 2026-11-08 — P1-01 SourceSpec + registry YAML (9752028).
- 2026-11-08 — P1-02 connecteur fichiers RawTable sans pandas (f1103ad). .gitignore : !tests/data/.
- 2026-11-08 — P1-03 connecteur REST httpx+tenacity, Retry-After en secondes (fa20c06).
- 2026-11-08 — P1-04 connecteur SQL incrémental + WatermarkStore, SQLite/Postgres (ce64f40).
- 2026-11-08 — P1-05 Bronze writer (LocalStore/S3Store SigV4 stdlib, put_if_absent, manifeste après parquet) (480ab40).
- 2026-11-08 — P1-06 dead-letter jsonl idempotent, read_file_with_rejects, seuil 20 % (7133a9c). REST/SQL/xlsx non branchés.
- 2026-11-08 — P1-07 simulateur de dérive v1→v3 (add/rename/type/drop), stdlib, déterministe (f0f5a4f).
- 2026-11-08 — P1-08 lifecycle Bronze + CLI adp (archivage par déplacement, manifeste en dernier) (a6f02dd). S3Store list/move sans test automatisé.
