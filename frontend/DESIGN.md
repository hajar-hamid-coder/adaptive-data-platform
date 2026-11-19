# Design system ADP : interface de la plateforme de données

Statut : proposition P5-05, à valider à la gate de la phase P5. Hypothèse H-10 : React + Vite + TypeScript + Tailwind, thème sobre clair/sombre, animations sobres (Framer Motion en P5-07).
Maquettes : `frontend/mockups/index.html` (4 écrans, clair et sombre). Source de vérité des tokens : `frontend/mockups/tokens.css`. Contrastes : `python3 scripts/check_contrast.py` (testé par `tests/test_contrast.py`).

## 1. Principes

1. **Lisibilité des données d'abord** : densité adaptée (tableaux compacts, chiffres tabulaires), pas de décor.
2. **L'état n'est jamais porté par la couleur seule** : chaque état a une icône (✓ ! ✕ i ◆) et un libellé ; les barres de score ajoutent la valeur et une texture (hachures) en plus de la teinte.
3. **L'humain intervient sur l'ambigu** : la revue du mapping et les changements de schéma bloquants sont mis en avant, le reste reste discret.
4. **Sobre et crédible** : une seule couleur d'accent, états sémantiques réservés aux états. Animations courtes, jamais indispensables.
5. **Mobile à 375 px sans défilement horizontal de la page** ; les tableaux défilent dans leur conteneur.

## 2. Tokens

Tous les tokens sont des variables CSS `--adp-*`. Le thème sombre s'active par `prefers-color-scheme` ou par `data-theme="dark"` sur `<html>` ; `data-theme="light"` force le clair.

### 2.1 Couleurs sémantiques

| Token | Clair | Sombre | Usage |
|---|---|---|---|
| `--adp-bg` | #f6f7f9 | #0f141a | fond de page |
| `--adp-surface` | #ffffff | #171d25 | cartes, tableaux, nav |
| `--adp-surface-2` | #edf0f4 | #212934 | entêtes de tableau, survol, pistes |
| `--adp-border` | #d3d9e1 | #2e3845 | séparateurs décoratifs |
| `--adp-border-strong` | #6b7685 | #8592a3 | bordures de champs et contrôles (≥ 3:1) |
| `--adp-text` | #18202b | #e8ecf1 | texte principal |
| `--adp-text-muted` | #4a5565 | #a5afbc | texte secondaire |
| `--adp-accent` | #1d5fd1 | #7aadff | actions, liens, navigation active |
| `--adp-on-accent` | #ffffff | #0b1220 | texte sur accent |
| `--adp-focus` | #1d5fd1 | #7aadff | anneau de focus |
| `--adp-ok` / `-bg` | #11683a / #e2f4ea | #5fcf93 / #12301f | promu, conforme, compatible |
| `--adp-warn` / `-bg` | #7f4d00 / #fdf0d5 | #f2b84f / #36280e | à revoir, sous seuil |
| `--adp-error` / `-bg` | #b0221a / #fce8e6 | #ff8f86 / #3b1816 | bloquant, échec |
| `--adp-info` / `-bg` | #0a5f7a / #e0f2f8 | #66c8e6 / #0f2f3a | information, en attente |
| `--adp-quarantine` / `-bg` | #6a3bbd / #efe8fb | #bea2ff / #2a1f45 | lignes isolées |

Correspondance sémantique des métiers : promu = ok ; COMPATIBLE = ok ; REVIEW = warn ; BLOCKING = error ; quarantaine = quarantine ; en attente / info = info.

### 2.2 Couleurs de graphiques

`--adp-chart-1` à `-5` (bleu, ambre, sarcelle, magenta, gris). Règle : **teinte + second codage** obligatoire (forme de marqueur, motif de trait ou de remplissage, étiquette directe). Maximum 5 séries ; au-delà, regrouper.

| Série | Clair | Sombre | Second codage |
|---|---|---|---|
| chart-1 | #1d5fd1 | #7aadff | cercle, trait plein |
| chart-2 | #b45309 | #f2a65a | carré, tirets |
| chart-3 | #0f766e | #4fd1c1 | triangle, points |
| chart-4 | #a21caf | #e08cf0 | losange, tirets-points |
| chart-5 | #5b6472 | #a5afbc | croix, trait fin |

### 2.3 Typographie

- Sans : `--adp-font-sans` = pile système (`system-ui, -apple-system, "Segoe UI", Roboto…`). Aucun chargement de police.
- Mono (identifiants, empreintes, règles) : `--adp-font-mono` = `ui-monospace, "SF Mono", Menlo, Consolas…`.
- Échelle : xs 0,75 rem · sm 0,875 · base 1 · lg 1,125 · xl 1,375 · 2xl 1,75. Interligne 1,5 (texte), 1,25 (titres). Chiffres des KPI et tableaux en `font-variant-numeric: tabular-nums`. Corps de tableau : sm.

### 2.4 Espacement, rayons, ombres, mouvement, points de rupture

| Famille | Tokens |
|---|---|
| Espacement (base 4 px) | `--adp-space-1..7` = 4, 8, 12, 16, 24, 32, 48 px |
| Rayons | `--adp-radius-sm/md/lg/full` = 4, 8, 12, 999 px |
| Ombre | `--adp-shadow` (légère, unique niveau ; plus marquée en sombre) |
| Durées | `--adp-dur-fast/base/slow` = 120, 200, 320 ms |
| Easing | `--adp-ease` = `cubic-bezier(0.2, 0, 0, 1)` |
| Points de rupture | sm 640 · md 900 (nav latérale ↔ barre haute) · lg 1200 px |

Animations autorisées : survol/focus (fast), transition de largeur d'une barre (slow), apparition de toast et de panneau (base), squelette de chargement. Interdit : rebonds, parallaxe, animation en boucle hors squelette. Sous `prefers-reduced-motion: reduce` : toute animation et transition est désactivée (déjà en place dans les maquettes ; en React, `useReducedMotion()` de Framer Motion).

### 2.5 Table de correspondance Tailwind (P5-06)

`tailwind.config` : `theme.extend.colors` référence les variables (`bg: 'var(--adp-bg)'`), `darkMode: ['selector', '[data-theme="dark"]']` pour le choix manuel, le défaut suivant `prefers-color-scheme`.

| Token | Clé de config | Classes |
|---|---|---|
| `--adp-bg` | `colors.bg` | `bg-bg` |
| `--adp-surface`, `-2` | `colors.surface`, `surface-2` | `bg-surface`, `bg-surface-2` |
| `--adp-border`, `-strong` | `colors.border`, `border-strong` | `border-border`, `border-border-strong` |
| `--adp-text`, `-muted` | `colors.fg`, `fg-muted` | `text-fg`, `text-fg-muted` |
| `--adp-accent`, `on-accent` | `colors.accent`, `on-accent` | `bg-accent text-on-accent`, `text-accent` |
| `--adp-focus` | `colors.focus` | `ring-focus`, `outline-focus` |
| `--adp-ok/warn/error/info/quarantine` (+ `-bg`) | `colors.ok`, `ok-bg`, … | `text-ok bg-ok-bg`, … |
| `--adp-chart-1..5` | `colors.chart.1..5` | `fill-chart-1`, `stroke-chart-2` |
| `--adp-font-sans/mono` | `fontFamily.sans/mono` | `font-sans`, `font-mono` |
| `--adp-text-xs..2xl` | `fontSize` | `text-xs … text-2xl` |
| `--adp-space-1..7` | `spacing` (4 px par pas, défaut Tailwind) | `p-1…p-12` (1=4 px, 2=8, 3=12, 4=16, 6=24, 8=32, 12=48) |
| `--adp-radius-sm/md/lg/full` | `borderRadius` | `rounded-sm/md/lg/full` |
| `--adp-shadow` | `boxShadow.card` | `shadow-card` |
| `--adp-dur-*`, `--adp-ease` | `transitionDuration`, `transitionTimingFunction` | `duration-fast/base/slow`, `ease-adp` |
| breakpoints | `screens` | `sm:`(640) `md:`(900) `lg:`(1200) |

## 3. Composants

Composants montrés dans les maquettes (écran entre parenthèses), sauf le graphe de lignage (spécifié seulement, P5-07).

| Composant | Contrat |
|---|---|
| **Nav latérale** (tous) | `<nav aria-label>` + liste ; élément actif : `aria-current="page"` + fond + liseré (pas la couleur seule). Cibles 44 px. Sous 900 px : barre haute en grille. Lien d'évitement « Aller au contenu ». |
| **Carte KPI** (pipeline, qualité) | libellé sm muted, valeur 2xl tabulaire, indice optionnel avec badge. Pas d'icône décorative. |
| **Tableau dense paginé** (pipeline, qualité, schémas) | `<table>` natif avec `<caption>`, `<th scope>`, conteneur `role="region"` + `aria-label` + `tabindex="0"` (défilement clavier), colonnes numériques alignées à droite, pagination `{items,total,limit,offset}` : « 1 à 4 sur N », Précédent/Suivant désactivés aux bornes. |
| **Badge de statut** (tous) | icône + libellé + bordure ; 5 variantes ok/warn/error/info/quarantaine. Paires texte/fond validées AA (§5). |
| **Barre de score qualité** (pipeline, qualité, revue) | piste + remplissage + **valeur numérique** ; seuils ≥ 90 plein (ok), 75-90 hachures diagonales (warn), < 75 hachures verticales (error) ; `role="img"` + `aria-label="Score 81 sur 100"`. |
| **Diff de schéma** (schémas) | liste ; signe `+` `−` `~` `≠` (ajout, suppression, renommage, changement de type) + nom de colonne en mono + détail + badge d'impact COMPATIBLE/REVIEW/BLOCKING, tri par gravité. Le détail d'un renommage montre le score et ses composantes. |
| **File de revue** (revue mapping) | liste de cartes : paire source → cible, barre de confiance, méthode, origine, justification, alternatives (`<details>`), actions Approuver (primaire), Rejeter, Choisir une autre cible. Après décision : badge de résultat, actions masquées, toast `role="status"`. |
| **Timeline de pipeline** (pipeline) | liste ordonnée Bronze → Silver → Gold ; chaque étape : nom, badge, heure ; liseré gauche coloré + badge ; étape non atteinte en pointillé « En attente ». |
| **Graphe de lignage** | liste horizontale de nœuds mono reliés par des flèches : `colonne Gold ← Silver ← Bronze ← fichier source` (alimenté par `GET /catalog/lineage`) ; sous 640 px, passage à la ligne ; un vrai graphe SVG n'est pas prévu au MVP. |
| **États vide / chargement / erreur** (pipeline) | vide : texte + action suggérée, cadre pointillé ; chargement : squelettes (pulsation désactivée en reduced-motion) ; erreur : `role="alert"`, message sans détail technique sensible, bouton Réessayer ; 401 : « Clé d'API invalide ou absente ». |
| **Toast** (revue) | `role="status"`, coin bas-droit, 5 s, fermable au clavier, jamais seul porteur d'une information critique. |
| **Boutons / champs** | hauteur 44 px (36 px dense en tableau, 44 px sur tactile), bordure `border-strong`, libellé `<label for>` visible. |

## 4. Écrans (maquettes)

| Écran | Fichier | Données API |
|---|---|---|
| Pipeline : KPI, état Bronze/Silver/Gold par source, dernier run, score, statut promu/quarantaine | `pipeline.html` | `/runs`, `/quality/scores` |
| Qualité : score global et par colonne, règles en échec, quarantaine (sans valeurs brutes), rapport | `qualite.html` | `/quality/scores`, `/quality/reports/{run_id}`, `/quality/quarantine/{source}` |
| Schémas : versions, diff v→v+1 avec impact | `schemas.html` | `/schemas/{source}`, `/schemas/{source}/diff` |
| Revue du mapping : file de propositions | `revue-mapping.html` | écriture à prévoir (voir limites) |

Hors maquettes, prévus en P5-07 avec les mêmes composants : catalogue et lignage (`/catalog`), produits (`/products`, SLA, criticité).

## 5. Accessibilité

- **Contraste** : texte ≥ 4,5:1, éléments d'interface et graphiques ≥ 3:1, vérifiés en clair et en sombre (tableau ci-dessous, rejouable).
- **Focus** : `:focus-visible` = anneau 2 px `--adp-focus`, décalage 2 px, jamais supprimé.
- **Clavier** : tout est atteignable au Tab dans l'ordre visuel ; Entrée/Espace sur les boutons ; Échap ferme dialogues et toasts ; conteneurs de tableau focalisables pour défiler.
- **Cibles tactiles** ≥ 44 × 44 px (36 px autorisé seulement pour les contrôles denses sur pointeur fin).
- **ARIA** : tableaux natifs (`caption`, `scope`) ; dialogues `role="dialog"` + `aria-modal` + `aria-labelledby`, focus piégé puis rendu au déclencheur ; alertes `role="alert"`, confirmations `role="status"` ; navigation `aria-label` ; `aria-current="page"`.
- **Couleur** : jamais seule (icône + libellé, texture des barres, signes du diff).
- **Mouvement** : `prefers-reduced-motion: reduce` désactive animations et transitions ; aucune information ne dépend d'une animation.
- **Langue** : `lang="fr"` ; zoom 200 % sans perte ; pas de texte dans des images.

## 6. Règles de contenu

- Libellés en français, phrases courtes, verbes d'action (« Approuver », « Réessayer »). Identifiants techniques (colonnes, règles, run_id) en mono, non traduits.
- Statuts : promu, quarantaine, bloqué, à revoir, compatible, bloquant, en attente. Même mot partout.
- Nombres : séparateur décimal virgule, espace avant `%`, heures `HH:MM` en UTC avec suffixe à l'export, dates ISO dans les tableaux.
- Messages d'erreur : ce qui s'est passé + quoi faire, sans trace ni valeur brute. Jamais de donnée réelle sensible affichée (la quarantaine n'expose que index et motifs).
- Les propositions LLM sont toujours présentées comme des propositions, avec méthode et justification.

## 7. Ratios de contraste WCAG (généré par `scripts/check_contrast.py --md`)

Seuils : 4,5:1 texte normal ; 3:1 éléments d'interface et séries de graphique.

| Thème | Premier plan | Fond | Ratio | Seuil | Usage | |
|---|---|---|---|---|---|---|
| clair | text | surface | 16.40 | 4.5 | texte courant | OK |
| clair | text | bg | 15.30 | 4.5 | texte courant | OK |
| clair | text | surface-2 | 14.35 | 4.5 | texte courant | OK |
| clair | text-muted | surface | 7.56 | 4.5 | texte secondaire | OK |
| clair | text-muted | bg | 7.05 | 4.5 | texte secondaire | OK |
| clair | text-muted | surface-2 | 6.61 | 4.5 | texte secondaire | OK |
| clair | on-accent | accent | 5.82 | 4.5 | texte de bouton primaire | OK |
| clair | accent | surface | 5.82 | 4.5 | lien / texte accent | OK |
| clair | accent | bg | 5.43 | 4.5 | lien / texte accent | OK |
| clair | focus | surface | 5.82 | 3.0 | anneau de focus | OK |
| clair | focus | bg | 5.43 | 3.0 | anneau de focus | OK |
| clair | border-strong | surface | 4.61 | 3.0 | bordure de champ / contrôle | OK |
| clair | border-strong | bg | 4.30 | 3.0 | bordure de champ / contrôle | OK |
| clair | ok | ok-bg | 5.99 | 4.5 | badge ok | OK |
| clair | warn | warn-bg | 6.28 | 4.5 | badge warn | OK |
| clair | error | error-bg | 5.77 | 4.5 | badge error | OK |
| clair | info | info-bg | 6.22 | 4.5 | badge info | OK |
| clair | quarantine | quarantine-bg | 5.92 | 4.5 | badge quarantine | OK |
| clair | ok | surface | 6.85 | 4.5 | texte ok | OK |
| clair | warn | surface | 7.09 | 4.5 | texte warn | OK |
| clair | error | surface | 6.80 | 4.5 | texte error | OK |
| clair | info | surface | 7.17 | 4.5 | texte info | OK |
| clair | quarantine | surface | 7.06 | 4.5 | texte quarantine | OK |
| clair | chart-1 | surface | 5.82 | 3.0 | série de graphique | OK |
| clair | chart-2 | surface | 5.02 | 3.0 | série de graphique | OK |
| clair | chart-3 | surface | 5.47 | 3.0 | série de graphique | OK |
| clair | chart-4 | surface | 6.32 | 3.0 | série de graphique | OK |
| clair | chart-5 | surface | 5.98 | 3.0 | série de graphique | OK |
| sombre | text | surface | 14.29 | 4.5 | texte courant | OK |
| sombre | text | bg | 15.59 | 4.5 | texte courant | OK |
| sombre | text | surface-2 | 12.36 | 4.5 | texte courant | OK |
| sombre | text-muted | surface | 7.63 | 4.5 | texte secondaire | OK |
| sombre | text-muted | bg | 8.33 | 4.5 | texte secondaire | OK |
| sombre | text-muted | surface-2 | 6.61 | 4.5 | texte secondaire | OK |
| sombre | on-accent | accent | 8.25 | 4.5 | texte de bouton primaire | OK |
| sombre | accent | surface | 7.47 | 4.5 | lien / texte accent | OK |
| sombre | accent | bg | 8.15 | 4.5 | lien / texte accent | OK |
| sombre | focus | surface | 7.47 | 3.0 | anneau de focus | OK |
| sombre | focus | bg | 8.15 | 3.0 | anneau de focus | OK |
| sombre | border-strong | surface | 5.36 | 3.0 | bordure de champ / contrôle | OK |
| sombre | border-strong | bg | 5.85 | 3.0 | bordure de champ / contrôle | OK |
| sombre | ok | ok-bg | 7.38 | 4.5 | badge ok | OK |
| sombre | warn | warn-bg | 8.01 | 4.5 | badge warn | OK |
| sombre | error | error-bg | 7.17 | 4.5 | badge error | OK |
| sombre | info | info-bg | 7.37 | 4.5 | badge info | OK |
| sombre | quarantine | quarantine-bg | 7.10 | 4.5 | badge quarantine | OK |
| sombre | ok | surface | 8.74 | 4.5 | texte ok | OK |
| sombre | warn | surface | 9.48 | 4.5 | texte warn | OK |
| sombre | error | surface | 7.69 | 4.5 | texte error | OK |
| sombre | info | surface | 8.85 | 4.5 | texte info | OK |
| sombre | quarantine | surface | 7.91 | 4.5 | texte quarantine | OK |
| sombre | chart-1 | surface | 7.47 | 3.0 | série de graphique | OK |
| sombre | chart-2 | surface | 8.38 | 3.0 | série de graphique | OK |
| sombre | chart-3 | surface | 9.06 | 3.0 | série de graphique | OK |
| sombre | chart-4 | surface | 7.41 | 3.0 | série de graphique | OK |
| sombre | chart-5 | surface | 7.63 | 3.0 | série de graphique | OK |

Plus bas ratio : 4,30:1 (`border-strong` sur `bg`, clair), seuil 3:1.

## 8. Limites et points à valider à la gate

- Maquettes statiques : données fictives, actions de revue simulées par un JS minimal local (`theme.js`) ; pas de graphiques SVG (les séries `chart-*` sont définies et validées mais non maquettées).
- Le calcul AA couvre les paires de tokens, pas chaque combinaison possible ; un audit axe/Lighthouse reste à faire sur l'UI réelle (P5-06/07).
- L'API est en lecture seule : la revue du mapping (approuver/rejeter) suppose un endpoint d'écriture à ajouter (hypothèse à consigner avant P5-07).
- Choix à valider : palette (bleu/ardoise sobre), pile de polices système, nav latérale, thème par défaut suivant le système.
