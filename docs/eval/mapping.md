# Évaluation du mapping (P3-09)

Rapport généré par `python -m adp.mapping.eval` (ne pas éditer à la main).

## Résumé

- Jeu annoté : 140 colonnes (82 dev, 58 holdout ; 37 sans correspondance).
- `rules`, holdout, proposé (>= 0.60) : précision 0.857, rappel 0.857, faux positifs sur unmappable 4/19.
- `rules+local`, holdout, proposé (>= 0.60) : précision 0.800, rappel 0.914, faux positifs 5/19.
- **règles+LLM : NON MESURÉ (ANTHROPIC_API_KEY absent)**.
- Critère du backlog (précision règles+LLM >= précision règles) : **NON VÉRIFIÉ** (le LLM n'a pas été mesuré) : ce rapport n'affirme pas qu'il est satisfait.

## Méthodologie et limites

- Jeu écrit par l'équipe, annotateur unique, **sans double annotation** : les annotations discutables sont en catégorie `ambiguous` avec une note.
- Split `dev` / `holdout` : le holdout n'a servi qu'à cette mesure, sans réglage des règles, des seuils ni du dictionnaire. L'annotateur connaissait toutefois le dictionnaire : les catégories `easy` mesurent surtout la couverture des synonymes.
- Les 30 colonnes bruitées de `tests/mapping/test_rules.py` sont incluses en `dev` (déjà vues au réglage des règles) : le dev est **optimiste**, le holdout fait foi.
- Jeu de petite taille : les intervalles de confiance (Wilson 95 %) sont larges, ne pas sur-interpréter une différence de quelques points. Les colonnes `ambiguous` sont comptées dans la précision et le rappel mais pas dans le taux de faux positifs.
- Deux points d'opération : *proposé* (rang 1 avec score >= 0.60, seuil de revue H-02) et *auto-appliqué* (score >= 0.90). Sous le seuil la prédiction est « (aucune) ».
- Précision = part correcte parmi les colonnes pour lesquelles une cible est proposée ; rappel = part retrouvée parmi les colonnes avec cible attendue ; faux positifs = cible proposée sur une colonne `unmappable`. Seuls des noms de colonnes sont utilisés, aucune valeur de données.
- `rules+local` : cascade de `LLMMapper` avec le repli local (aucun LLM, aucun réseau). Il ne donne aucune indication sur la valeur du LLM.
- `rules+llm` n'est mesuré qu'avec une clé API (plafond `--max-llm-columns` et budget de `llm.py`) ; aucune valeur n'est simulée ni extrapolée.

## Jeu annoté

| Catégorie | n |
|---|---|
| easy | 35 |
| noisy | 59 |
| ambiguous | 9 |
| unmappable | 37 |

## Résultats

### Point « proposé » (score >= 0.60)

| Mode | Sous-ensemble | n | Proposées | Précision | IC95 préc. | Rappel | IC95 rappel | F1 | Faux positifs (unmappable) | Top-1 | Couverture |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rules | holdout | 58 | 35 | 0.857 | [0.71 ; 0.94] | 0.857 | [0.71 ; 0.94] | 0.857 | 4/19 | 0.845 | 0.603 |
| rules | dev | 82 | 62 | 0.952 | [0.87 ; 0.98] | 0.967 | [0.89 ; 0.99] | 0.959 | 2/18 | 0.951 | 0.756 |
| rules | all | 140 | 97 | 0.918 | [0.85 ; 0.96] | 0.927 | [0.86 ; 0.96] | 0.922 | 6/37 | 0.907 | 0.693 |
| rules+local | holdout | 58 | 40 | 0.800 | [0.65 ; 0.90] | 0.914 | [0.78 ; 0.97] | 0.853 | 5/19 | 0.828 | 0.690 |
| rules+local | dev | 82 | 65 | 0.923 | [0.83 ; 0.97] | 0.984 | [0.91 ; 1.00] | 0.952 | 2/18 | 0.939 | 0.793 |
| rules+local | all | 140 | 105 | 0.876 | [0.80 ; 0.93] | 0.958 | [0.90 ; 0.98] | 0.915 | 7/37 | 0.893 | 0.750 |

Par catégorie (jeu complet) :

| Mode | Sous-ensemble | n | Proposées | Précision | IC95 préc. | Rappel | IC95 rappel | F1 | Faux positifs (unmappable) | Top-1 | Couverture |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rules | cat:easy | 35 | 35 | 1.000 | [0.90 ; 1.00] | 1.000 | [0.90 ; 1.00] | 1.000 | - | 1.000 | 1.000 |
| rules | cat:noisy | 59 | 54 | 0.981 | [0.90 ; 1.00] | 0.898 | [0.80 ; 0.95] | 0.938 | - | 0.898 | 0.915 |
| rules | cat:ambiguous | 9 | 2 | 0.500 | [0.09 ; 0.91] | 0.500 | [0.09 ; 0.91] | 0.500 | - | 0.889 | 0.222 |
| rules | cat:unmappable | 37 | 6 | 0.000 | [0.00 ; 0.39] | n/a | n/a | 0.000 | 6/37 | 0.838 | 0.162 |
| rules+local | cat:easy | 35 | 35 | 1.000 | [0.90 ; 1.00] | 1.000 | [0.90 ; 1.00] | 1.000 | - | 1.000 | 1.000 |
| rules+local | cat:noisy | 59 | 57 | 0.982 | [0.91 ; 1.00] | 0.949 | [0.86 ; 0.98] | 0.966 | - | 0.949 | 0.966 |
| rules+local | cat:ambiguous | 9 | 6 | 0.167 | [0.03 ; 0.56] | 0.500 | [0.09 ; 0.91] | 0.250 | - | 0.444 | 0.667 |
| rules+local | cat:unmappable | 37 | 7 | 0.000 | [0.00 ; 0.35] | n/a | n/a | 0.000 | 7/37 | 0.811 | 0.189 |

### Point « auto-appliqué » (score >= 0.90)

| Mode | Sous-ensemble | n | Proposées | Précision | IC95 préc. | Rappel | IC95 rappel | F1 | Faux positifs (unmappable) | Top-1 | Couverture |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rules | holdout | 58 | 20 | 1.000 | [0.84 ; 1.00] | 0.571 | [0.41 ; 0.72] | 0.727 | 0/19 | 0.741 | 0.345 |
| rules | dev | 82 | 38 | 1.000 | [0.91 ; 1.00] | 0.623 | [0.50 ; 0.73] | 0.768 | 0/18 | 0.720 | 0.463 |
| rules | all | 140 | 58 | 1.000 | [0.94 ; 1.00] | 0.604 | [0.50 ; 0.70] | 0.753 | 0/37 | 0.729 | 0.414 |
| rules+local | holdout | 58 | 20 | 1.000 | [0.84 ; 1.00] | 0.571 | [0.41 ; 0.72] | 0.727 | 0/19 | 0.741 | 0.345 |
| rules+local | dev | 82 | 38 | 1.000 | [0.91 ; 1.00] | 0.623 | [0.50 ; 0.73] | 0.768 | 0/18 | 0.720 | 0.463 |
| rules+local | all | 140 | 58 | 1.000 | [0.94 ; 1.00] | 0.604 | [0.50 ; 0.70] | 0.753 | 0/37 | 0.729 | 0.414 |

## Règles + LLM

**règles+LLM : NON MESURÉ (ANTHROPIC_API_KEY absent).**

Le chemin réel est implémenté et testé avec un fournisseur simulé (plomberie uniquement). Pour le mesurer : exporter `ANTHROPIC_API_KEY` puis relancer la commande ; le plafond `--max-llm-columns` et le budget mensuel de `llm.py` s'appliquent. Aucun chiffre n'est affiché ici faute de mesure.

## Matrice de confusion (mode `rules`, point proposé, jeu complet)

Classes présentes uniquement ; `.` = 0 ; `(aucune)` = pas de proposition / pas de cible.

| attendu \ prédit | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P9 | P10 | P11 | P12 | P13 | P14 | P15 | P16 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| (aucune) | 38 | 1 | 3 | . | . | . | . | . | . | . | . | 2 | . | . | . | . |
| activity_rate | 1 | 3 | . | . | . | . | . | . | . | . | . | . | . | . | . | . |
| country_code | . | . | 8 | . | . | . | . | . | . | . | . | . | . | . | . | . |
| country_name | . | . | . | 7 | . | . | . | . | . | . | . | . | . | . | . | . |
| gdp_current_usd | . | . | . | . | 7 | . | . | . | . | . | . | . | . | . | . | . |
| gdp_per_capita_usd | . | . | . | . | . | 4 | . | . | . | . | . | . | . | . | . | . |
| indicator_code | . | . | . | . | . | . | 3 | . | . | . | . | . | . | . | . | . |
| indicator_name | . | . | . | . | . | . | 1 | 4 | . | . | . | . | . | . | . | . |
| inflation_rate | 1 | . | . | . | . | . | . | . | 5 | . | . | . | . | . | . | . |
| life_expectancy | . | . | . | . | . | . | . | . | . | 7 | . | . | . | . | . | . |
| literacy_rate | . | . | . | . | . | . | . | . | . | . | 5 | . | . | . | . | . |
| period | . | . | . | . | . | . | . | . | . | . | . | 4 | . | . | 1 | . |
| population_total | 1 | . | . | . | . | . | . | . | . | . | . | . | 12 | . | . | . |
| region | . | . | . | . | . | . | . | . | . | . | . | . | . | 5 | . | . |
| unemployment_rate | . | . | . | . | . | . | . | . | . | . | . | . | . | . | 11 | . |
| year | 2 | . | . | . | . | . | . | . | . | . | . | . | . | . | . | 4 |

Colonnes : P1 = (aucune) ; P2 = activity_rate ; P3 = country_code ; P4 = country_name ; P5 = gdp_current_usd ; P6 = gdp_per_capita_usd ; P7 = indicator_code ; P8 = indicator_name ; P9 = inflation_rate ; P10 = life_expectancy ; P11 = literacy_rate ; P12 = period ; P13 = population_total ; P14 = region ; P15 = unemployment_rate ; P16 = year.

### Confusions hors diagonale

- (aucune) -> activity_rate : 1
- (aucune) -> country_code : 3
- (aucune) -> period : 2
- activity_rate -> (aucune) : 1
- indicator_name -> indicator_code : 1
- inflation_rate -> (aucune) : 1
- period -> unemployment_rate : 1
- population_total -> (aucune) : 1
- year -> (aucune) : 2

## Erreurs détaillées (mode `rules`, point proposé)

| Colonne | Attendu | Prédit | Score | Méthode | Catégorie | Split |
|---|---|---|---|---|---|---|
| `Année de référence` | year | (aucune) | 0.000 | - | noisy | dev |
| `obs_date` | period | unemployment_rate | 0.658 | fuzzy | noisy | dev |
| `mot_de_passe` | (aucune) | country_code | 0.627 | fuzzy | unmappable | dev |
| `code_postal` | (aucune) | country_code | 0.658 | fuzzy | unmappable | dev |
| `SL.TLF.CACT.ZS` | activity_rate | (aucune) | 0.000 | - | noisy | holdout |
| `Pop. résidente` | population_total | (aucune) | 0.000 | - | noisy | holdout |
| `Année d'enquête` | year | (aucune) | 0.000 | - | noisy | holdout |
| `Inflation, prix à la consommation (% annuel)` | inflation_rate | (aucune) | 0.000 | - | noisy | holdout |
| `indicateur` | indicator_name | indicator_code | 0.817 | fuzzy | ambiguous | holdout |
| `date_creation` | (aucune) | period | 0.713 | fuzzy | unmappable | holdout |
| `date_livraison` | (aucune) | period | 0.627 | fuzzy | unmappable | holdout |
| `taux_tva` | (aucune) | activity_rate | 0.627 | fuzzy | unmappable | holdout |
| `code_produit` | (aucune) | country_code | 0.627 | fuzzy | unmappable | holdout |

### Écarts de `rules+local` par rapport à `rules` (point proposé)

Erreurs en moins : 3 ; erreurs en plus : 5.

| Colonne | Attendu | Prédit | Score | Méthode | Catégorie | Split |
|---|---|---|---|---|---|---|
| `value` | (aucune) | country_name | 0.676 | local | ambiguous | dev |
| `code` | (aucune) | country_code | 0.661 | local | ambiguous | dev |
| `nom` | (aucune) | country_name | 0.620 | local | ambiguous | holdout |
| `total` | (aucune) | population_total | 0.709 | local | ambiguous | holdout |
| `annee_naissance` | (aucune) | year | 0.631 | local | unmappable | holdout |

## Pistes d'amélioration observées

Pistes déduites des erreurs ci-dessus (non appliquées ici : le holdout ne sert pas à régler les règles ; correction éventuelle = autre tâche).

- Colonnes attendues sans proposition (5) : `Année de référence`, `SL.TLF.CACT.ZS`, `Pop. résidente`, `Année d'enquête`, `Inflation, prix à la consommation (% annuel)`. Piste : synonymes, abréviations ou formulations absents du dictionnaire (ou LLM pour les cas non lexicaux).
- Mauvaise cible proposée (2) : `obs_date`, `indicateur`. Piste : départager par le type inféré et le profil, ou par une revue humaine.
- Faux positifs sur colonnes sans correspondance ou ambiguës (6) : `mot_de_passe`, `code_postal`, `date_creation`, `date_livraison`, `taux_tva`, `code_produit`. Piste : relever le seuil de revue pour le fuzzy ou étendre la liste des jetons génériques.
