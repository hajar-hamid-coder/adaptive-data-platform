---
paths:
  - "frontend/**"
---

# Règles front-end

- Respecter `frontend/DESIGN.md` (tokens couleur/typo/espacement) ; pas de couleurs ou tailles codées en dur.
- TypeScript strict ; client API typé généré ou écrit depuis `/openapi.json`.
- États vide / chargement / erreur obligatoires pour chaque vue de données.
- Accessibilité : contraste AA, navigation clavier, libellés ; `prefers-reduced-motion` désactive les animations non essentielles.
- Animations sobres (≤ 250 ms), jamais bloquantes ; tests Vitest + Playwright, audit axe sans violation serious/critical.
