# Dispositif Claude Code du projet

## Vue d'ensemble
| Élément | Emplacement | Rôle |
|---|---|---|
| Contexte projet | `CLAUDE.md` | Mission, stack, flux de travail, conventions, sécurité — chargé à chaque session |
| Backlog | `backlog/backlog.xlsx` (+ `BACKLOG.md` généré) | 8 phases, 64 tâches, modèle par tâche, dépendances, hypothèses |
| Pilote du backlog | `scripts/backlog.py` | Seul point d'écriture ; refuse les transitions illégales, les dépendances non finies et les phases verrouillées |
| Skills | `.claude/skills/` | `/run-phase`, `/validate-phase`, `/add-hypothesis`, `portfolio-writer` |
| Sous-agents | `.claude/agents/` | `task-worker-opus/sonnet/haiku` (modèle imposé), `phase-reviewer` (lecture seule) |
| Hooks | `.claude/hooks/`, `.claude/settings.json` | Contexte au démarrage, garde-fous Bash/fichiers, format Python, relance en autopilot |
| Règles | `.claude/rules/` | Règles Python et front chargées selon les fichiers touchés |
| Mémoire | `.claude/memory/` | Profil, décisions, leçons, journal |
| Plugins | `.claude/settings.json` | ponytail, ui-ux-pro-max (installés à l'ouverture, après confirmation) ; graphify via P0-06 |

## Cycle d'une phase
```
/run-phase Pn  ──► tâche 1..N : En cours → sous-agent (Opus/Sonnet/Haiku) → test rejoué → commit → À valider
               └─► phase-reviewer → push phase/Pn → bilan → « Valides-tu ? » → STOP
/validate-phase Pn ──► Validé + gate Oui → merge main + tag → annonce Pn+1
```

## Hooks
- **SessionStart** : affiche l'avancement, la prochaine tâche, la mémoire et le journal.
- **PreToolUse Bash** : refuse `--no-verify`, force-push, `reset --hard`, `rm -rf` dangereux, `curl | sh`, accès shell à `.env`, édition directe du classeur, push `main` en autopilot.
- **PreToolUse Write/Edit** : protège `backlog.xlsx`, `.env*`, et (en autopilot) `settings.json` et `hooks/`.
- **PostToolUse Write/Edit** : `ruff format` + `ruff check --fix` sur les `.py`.
- **Stop** : en autopilot, relance Claude (5 fois max) tant qu'il reste une tâche non terminée.

## Plugins et sécurité
Les plugins exécutent du code avec tes droits : relire leur contenu avant d'accepter. Sources : [ponytail](https://github.com/DietrichGebert/ponytail), [ui-ux-pro-max](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill), [graphify](https://github.com/safishamsi/graphify) (`pip install graphifyy && graphify install`).

## Mode auto
`defaultMode` est `acceptEdits` avec une liste blanche de commandes sûres. Pour l'autonomie maximale, passer la session en mode auto (Maj+Tab dans le terminal, ou `claude --permission-mode auto`) : les garde-fous des hooks et du `deny` restent actifs.
