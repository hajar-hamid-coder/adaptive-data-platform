---
name: portfolio-writer
description: Transforme le travail réalisé en contenus de candidature (README, bullet points CV FR/EN, résumé LinkedIn, étude de cas) en s'appuyant uniquement sur des preuves du repo. À utiliser pour la phase P7 ou pour résumer une phase.
---

# Rédaction portfolio / CV

Profil cible : data engineer / analytics engineer avec casquettes data science, architecture, BA/BIA (voir `.claude/memory/user-profile.md`).

## Règles
- **Aucun chiffre inventé.** Chaque métrique cite sa source : commande reproductible, rapport dans `docs/`, ou commit. Sinon écrire « à mesurer » et proposer la mesure.
- Format bullet : **verbe d'action + quoi + comment (techno) + résultat mesuré**. 1-2 lignes max.
- Séparer ce qui est **réalisé** de ce qui est **prévu** ; le CV ne mentionne que du réalisé et vérifiable.
- Écrire en FR et EN ; ton sobre, pas de superlatifs.
- Une section BA/BIA : problème métier, parties prenantes, KPI, analyse d'impact, risques.
- Terminer par une table « compétence → preuve dans le repo (lien) ».
- Ne pas inventer d'expérience en entreprise : ce projet est un projet personnel/portfolio, à présenter comme tel.

## Sorties
`docs/career/cv-bullets.md`, `docs/career/linkedin.md`, `docs/career/skills-matrix.md`, `docs/case-study.md`.
