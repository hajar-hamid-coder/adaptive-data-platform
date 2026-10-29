---
name: add-hypothesis
description: Consigne une ambiguïté ou une précision manquante dans la feuille Hypotheses du backlog, avec l'hypothèse de travail retenue. À utiliser dès qu'une tâche n'est pas assez précise pour être implémentée sans deviner.
---

# Ajouter une hypothèse

Ne jamais deviner en silence. Quand une tâche manque de précision :

1. Vérifier d'abord `python3 scripts/backlog.py hypo` : l'hypothèse existe peut-être déjà.
2. Sinon :
```bash
python3 scripts/backlog.py hypo-add --tasks "P3-03" --question "<ce qui est ambigu>" \
  --assumption "<hypothèse de travail, concrète et réversible>" \
  --impact "<ce qui change si elle est fausse>" --ask "<la précision exacte à demander à l'utilisateur>"
```
3. Continuer la tâche sur l'hypothèse retenue si elle est réversible à faible coût ; sinon marquer la tâche bloquée dans les notes et remonter à l'utilisateur à la gate de phase.
4. Référencer le nouvel ID (H-xx) dans la colonne « Hypothèse » de la tâche (via une note) et dans le message de commit.
