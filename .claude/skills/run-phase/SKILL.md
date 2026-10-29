---
name: run-phase
description: Exécute en autonomie TOUTES les tâches d'une phase du backlog, dans l'ordre, puis s'arrête et demande la validation de l'utilisateur. Usage /run-phase P1.
argument-hint: "<phase, ex. P1>"
disable-model-invocation: true
---

# Exécuter une phase en autopilot

Phase demandée : `$ARGUMENTS`. Source de vérité : `backlog/backlog.xlsx`, **uniquement via** `python3 scripts/backlog.py`.

## 0. Pré-vérifications (stop si une échoue)
1. `python3 scripts/backlog.py status` — la phase demandée doit être la phase courante (▶). La précédente doit avoir sa gate validée. Sinon : expliquer et s'arrêter.
2. `python3 scripts/backlog.py hypo` — lire les hypothèses ouvertes qui concernent la phase. On travaille sur l'**hypothèse retenue** ; on ne bloque pas dessus.
3. Lire `CLAUDE.md` et `.claude/memory/MEMORY.md`. Si un `graphify-out/GRAPH_REPORT.md` existe, le lire avant d'explorer le code.
4. `git switch -c phase/<Pn>` (ou `git switch phase/<Pn>` si elle existe). Puis `mkdir -p .claude/state` et `touch .claude/state/autopilot`.

## 1. Boucle (répéter jusqu'à ce qu'il n'y ait plus de tâche Non fait / En cours)
1. `python3 scripts/backlog.py next` → détail de la tâche (objectif, code, test, modèle, hypothèse).
2. `python3 scripts/backlog.py set <ID> "En cours"` (refusé si une dépendance n'est pas terminée : corriger l'ordre, ne pas forcer).
3. **Déléguer au sous-agent correspondant à la colonne Modèle** : `task-worker-opus`, `task-worker-sonnet` ou `task-worker-haiku` (outil Agent). Lui transmettre : la sortie complète de `backlog.py show <ID>`, les hypothèses liées, et ce qui existe déjà dans le repo. Il écrit tests puis code, lance les tests, et rend un compte-rendu avec les preuves.
4. **Ne pas croire le compte-rendu** : relancer toi-même la commande de la colonne « Test de validation », plus `ruff check .` et `pytest -q` (ou `npm test` pour le front).
5. Si OK : `git add -A && git commit -m "feat(<ID>): <titre>"` (anglais, impératif), puis `python3 scripts/backlog.py set <ID> "À valider" --note "<sha court> ; <commande de test> -> <résultat>"`.
6. Si KO : renvoyer au sous-agent avec l'erreur exacte (max 3 essais). Après 3 échecs : `backlog.py hypo-add ...` pour documenter l'ambiguïté ou le blocage, noter dans la colonne notes, puis **arrêter la phase** et rapporter (ne pas contourner un test, ne pas affaiblir un critère).
7. Ajouter une ligne au `.claude/memory/journal.md` à chaque tâche (une ligne) ; décisions durables dans `decisions.md`, pièges dans `lessons.md`.

## 2. Fin de phase (toutes les tâches « À valider »)
1. Lancer le sous-agent `phase-reviewer` (lecture seule) : suite de tests complète, lint, cohérence avec le backlog, secrets, dette ouverte.
2. `python3 scripts/backlog.py export-md`, commit, `git push -u origin phase/<Pn>`.
3. `rm -f .claude/state/autopilot`.
4. Présenter à l'utilisateur, en français, de façon concise :
   - tableau des tâches (ID, statut, preuve), modèles utilisés ;
   - résultats des tests et du reviewer ;
   - hypothèses ouvertes à trancher, écarts, risques ;
   - ce que livre la phase suivante.
5. Terminer par la question : **« Valides-tu la phase <Pn> ? »** puis **S'ARRÊTER**.

## Interdits
- Ne jamais passer une tâche à `Validé`, ni lancer la phase suivante, ni fusionner dans `main` : c'est le rôle de `/validate-phase`, après accord explicite.
- Ne pas modifier `.claude/settings.json` ni `.claude/hooks/`. Ne pas éditer `backlog.xlsx` autrement que par `scripts/backlog.py`.
- Ne jamais envoyer de données réelles sensibles à un LLM ; pas de secrets dans le repo.
- Pas de dépendance ou de fonctionnalité hors périmètre de la tâche (voir plugin ponytail : écrire le minimum nécessaire).
