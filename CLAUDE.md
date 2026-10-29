# Plateforme de Données Intelligente et Évolutive (ADP)

Projet portfolio de Hajar Hamid, destiné à être présenté en candidature (data engineer / analytics engineer, avec angle data science, architecture et BA/BIA).

## Mission
Plateforme d'ingestion et de traitement qui s'adapte seule aux changements : *source → Bronze → Silver → Gold*, avec détection de schéma, auto-mapping (règles + LLM/RAG), gestion des évolutions, qualité et quarantaine, observabilité et auto-optimisation. Valeur : une nouvelle source ou un changement de schéma devient exploitable en heures, avec validation humaine uniquement sur les cas ambigus.

Schéma d'architecture de référence : `docs/architecture.md` (créé en P0-07). Le diagramme d'origine est celui de l'utilisateur (9 blocs de chaîne, médaillon, services/exposition, observabilité, auto-optimisation, techno Go/Rust/Python).

## Stack MVP (hypothèses H-03, H-04, H-06, H-08, H-10 — voir feuille Hypotheses)
Python 3.12 + uv · DuckDB + Parquet · MinIO (S3) · PostgreSQL (métadonnées) · Dagster · FastAPI · React/Vite/TS/Tailwind · Prometheus/Grafana · Docker Compose. Rust en option (P6-08). LLM : API Claude derrière une interface `LLMProvider` avec repli sans LLM.

## Source de vérité du travail : `backlog/backlog.xlsx`
- Feuille 1 `Ordre_Execution` : phases, avancement, ordre des 64 tâches. Une feuille par phase P0…P7. Feuille `Hypotheses`, feuille `Legende`.
- **N'accède au classeur que par** `python3 scripts/backlog.py` (`status`, `next`, `show ID`, `set ID STATUT --note`, `validate Pn`, `hypo`, `hypo-add`, `export-md`). Un hook interdit l'édition directe.
- Statuts : `Non fait` → `En cours` → `À valider`. `Validé` n'est posé que par `/validate-phase`, après accord de l'utilisateur.
- `backlog/BACKLOG.md` est généré depuis le classeur : ne pas l'éditer.

## Flux de travail (autonome, une phase à la fois)
1. L'utilisateur lance `/run-phase Pn`. Tu enchaînes **toutes** les tâches de la phase sans demander d'autorisation intermédiaire.
2. Chaque tâche : `set En cours` → déléguer au sous-agent du bon modèle (`task-worker-opus|sonnet|haiku`, colonne *Modèle*) → **revérifier toi-même** le test de validation → commit → `set À valider --note "<sha> ; test -> résultat"`.
3. Fin de phase : `phase-reviewer`, `export-md`, push de `phase/Pn`, bilan, question « Valides-tu la phase Pn ? », **puis arrêt**.
4. L'utilisateur lance `/validate-phase Pn` : validation, fusion dans `main`, tag, annonce de la suite. Ne jamais enchaîner sur la phase suivante seul.
5. Tâche imprécise : `/add-hypothesis` (ne pas deviner en silence), avancer sur l'hypothèse retenue si réversible, sinon remonter à la gate.

Choix du modèle : Opus = conception/algorithmes critiques/sécurité ; Sonnet = implémentation standard ; Haiku = échafaudage, config, doc. La session principale peut tourner sur Sonnet : les sous-agents portent le bon modèle.

## Conventions
- Code, identifiants, commits et noms de branches en **anglais** ; docs, backlog, rapports en **français**.
- Commits conventionnels `feat(P1-03): …`, un commit par tâche minimum. Branche `phase/Pn` par phase ; `main` ne reçoit que des phases validées.
- Tests d'abord (pytest ; Vitest/Playwright pour le front). Aucune tâche n'est « À valider » sans test vert rejoué.
- Écrire le minimum nécessaire (plugin **ponytail**) : réutiliser stdlib/existant, pas de couche ni dépendance spéculative.
- Avant d'explorer le code, lire `graphify-out/GRAPH_REPORT.md` (plugin/skill **graphify**) s'il existe ; le rafraîchir à la validation de phase.
- UI : suivre `frontend/DESIGN.md` et le skill **ui-ux-pro-max** ; animations sobres, `prefers-reduced-motion` respecté.
- Types partout, docstrings courts, logs structurés avec `run_id`, erreurs du domaine (`adp.errors`).

## Sécurité et limites
- Jamais de secret dans le repo ni dans un prompt ; `.env` interdit (`.env.example` factice). Pas de lecture de `.env`.
- Aucune donnée réelle sensible vers un LLM : seulement noms de colonnes et échantillons anonymisés (H-09). Les sorties LLM sont des **propositions** validées par schéma, jamais exécutées.
- Pas de `git push --force`, `reset --hard`, `rm -rf`, `--no-verify`. Pas de push sur `main` en autopilot. Ne pas modifier `.claude/settings.json` ni `.claude/hooks/` en autopilot.
- Dépôt GitHub privé jusqu'à décision explicite (H-13).

## Mémoire du projet (`.claude/memory/`)
`MEMORY.md` (index, lu au démarrage par le hook SessionStart), `user-profile.md`, `decisions.md`, `lessons.md`, `journal.md`. Écrire une ligne au journal par tâche ; consigner décisions durables et pièges. Ne pas y mettre ce que le code ou git disent déjà.

## Plugins & skills du projet
- Plugins (`.claude/settings.json`) : `ponytail@ponytail` (sobriété du code), `ui-ux-pro-max@ui-ux-pro-max-skill` (design UI). Skill/outil `graphify` (graphe de connaissance du code) installé en P0-06 via `scripts/setup_tools.sh`.
- Skills du repo : `run-phase`, `validate-phase`, `add-hypothesis`, `portfolio-writer`. Sous-agents : `task-worker-opus|sonnet|haiku`, `phase-reviewer`.
- « Motion skills » : voir H-10 (par défaut Framer Motion + règles reduced-motion).
