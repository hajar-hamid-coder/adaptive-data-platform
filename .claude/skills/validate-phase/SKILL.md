---
name: validate-phase
description: Clôture une phase APRÈS accord explicite de l'utilisateur - passe les tâches à Validé, fusionne la branche de phase dans main, tague et prépare la suivante. Usage /validate-phase P1.
argument-hint: "<phase, ex. P1>"
disable-model-invocation: true
---

# Valider une phase

L'utilisateur a invoqué cette commande : c'est son accord explicite pour la phase `$ARGUMENTS`.

1. `python3 scripts/backlog.py status` : toutes les tâches de la phase doivent être « À valider », la branche `phase/<Pn>` doit être propre (`git status`) et poussée. Sinon, dire ce qui manque et s'arrêter.
2. Si l'utilisateur a formulé des remarques en validant, les traiter d'abord (repasser les tâches concernées `En cours` via `backlog.py set`, corriger, retester) au lieu de valider.
3. `python3 scripts/backlog.py validate <Pn>` (tâches → Validé, gate = Oui) puis `python3 scripts/backlog.py export-md`.
4. Mettre à jour la mémoire : `journal.md` (bilan de phase), `decisions.md` (décisions prises), `lessons.md` (pièges). Si un `graphify-out/` existe, relancer `graphify .` pour rafraîchir le graphe.
5. Commit sur `phase/<Pn>` : `chore(<Pn>): validate phase`.
6. Fusion : `git switch main && git merge --no-ff phase/<Pn> -m "Merge <Pn>: <intitulé>"`, `git tag phase-<Pn>-validated`, puis `git push origin main --tags` (ces commandes demanderont ta confirmation : c'est voulu).
7. Annoncer en 3 lignes : phase validée, phase suivante (nom, nb de tâches, modèles dominants, hypothèses à trancher), et la commande à lancer : `/run-phase <Pn+1>`. **Ne pas la lancer.**
