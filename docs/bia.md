# Analyse d'impact (BIA) des data products

> Fichier généré par `uv run adp products bia` depuis `config/products/*.yaml` : ne pas éditer à la main.
> Owners et consommateurs sont des équipes et rôles fictifs (H-05).

Criticité effective d'un produit = max(criticité propre, criticité des produits aval). Une source amont hérite de la criticité effective maximale des produits construits dessus.

## Produits critiques

| Produit | Criticité propre | Owner | SLA fraîcheur | Consommateurs impactés |
|---|---|---|---|---|
| `population_by_region` | critique (`critical`) | Équipe Données démographiques | 168 h | 4 |

## Matrice produits × consommateurs

| Produit | Criticité effective | SLA fraîcheur | Consommateur | Usage | Dépendance | Via |
|---|---|---|---|---|---|---|
| `population_by_region` | critique (`critical`) | 168 h | Service Études régionales | Dénominateur des indicateurs par habitant. | forte | `population_by_region` |
| `population_by_region` | critique (`critical`) | 168 h | Tableau de bord Pilotage territorial | Suivi mensuel de la population par région pour l'allocation budgétaire. | forte | `population_by_region` |
| `population_by_region` | critique (`critical`) | 168 h | API publique de la plateforme | Exposition des indicateurs de synthèse (P5-03). | faible | `socioeconomic_summary` |
| `population_by_region` | critique (`critical`) | 168 h | Rapport annuel Direction de la stratégie | Chiffres clés publiés dans le rapport annuel. | forte | `socioeconomic_summary` |
| `socioeconomic_summary` | haute (`high`) | 744 h | API publique de la plateforme | Exposition des indicateurs de synthèse (P5-03). | faible | `socioeconomic_summary` |
| `socioeconomic_summary` | haute (`high`) | 744 h | Rapport annuel Direction de la stratégie | Chiffres clés publiés dans le rapport annuel. | forte | `socioeconomic_summary` |
| `gdp_indicators` | haute (`high`) | 744 h | Cellule Data science | Exploration ad hoc et notebooks de modélisation. | faible | `gdp_indicators` |
| `gdp_indicators` | haute (`high`) | 744 h | API publique de la plateforme | Exposition des indicateurs de synthèse (P5-03). | faible | `socioeconomic_summary` |
| `gdp_indicators` | haute (`high`) | 744 h | Rapport annuel Direction de la stratégie | Chiffres clés publiés dans le rapport annuel. | forte | `socioeconomic_summary` |

## Sources amont

| Source | Criticité effective | Produits dépendants |
|---|---|---|
| `hcp_population` | critique (`critical`) | `population_by_region`, `socioeconomic_summary` |
| `world_bank_gdp` | haute (`high`) | `gdp_indicators`, `socioeconomic_summary` |

## Détail par produit

### `population_by_region` (v1.0.0)

- Owner : Équipe Données démographiques (Product owner data)
- Criticité : propre critique (`critical`), effective critique (`critical`)
- SLA fraîcheur : 168 h
- Score qualité minimal : 90
- Chaîne amont : sources `hcp_population` ; produits aucun
- Produits aval : `socioeconomic_summary`

Si ce produit est indisponible, sont impactés :

- Service Études régionales : Dénominateur des indicateurs par habitant. (dépendance forte)
- Tableau de bord Pilotage territorial : Suivi mensuel de la population par région pour l'allocation budgétaire. (dépendance forte)
- API publique de la plateforme : Exposition des indicateurs de synthèse (P5-03). (dépendance faible, via `socioeconomic_summary`)
- Rapport annuel Direction de la stratégie : Chiffres clés publiés dans le rapport annuel. (dépendance forte, via `socioeconomic_summary`)

### `socioeconomic_summary` (v1.0.0)

- Owner : Équipe Analytics engineering (Product owner data)
- Criticité : propre haute (`high`), effective haute (`high`)
- SLA fraîcheur : 744 h
- Score qualité minimal : 85
- Chaîne amont : sources `hcp_population`, `world_bank_gdp` ; produits `gdp_indicators`, `population_by_region`
- Produits aval : aucun

Si ce produit est indisponible, sont impactés :

- API publique de la plateforme : Exposition des indicateurs de synthèse (P5-03). (dépendance faible)
- Rapport annuel Direction de la stratégie : Chiffres clés publiés dans le rapport annuel. (dépendance forte)

### `gdp_indicators` (v1.0.0)

- Owner : Équipe Indicateurs économiques (Analytics engineer référent)
- Criticité : propre faible (`low`), effective haute (`high`)
- SLA fraîcheur : 744 h
- Score qualité minimal : 80
- Chaîne amont : sources `world_bank_gdp` ; produits aucun
- Produits aval : `socioeconomic_summary`

Si ce produit est indisponible, sont impactés :

- Cellule Data science : Exploration ad hoc et notebooks de modélisation. (dépendance faible)
- API publique de la plateforme : Exposition des indicateurs de synthèse (P5-03). (dépendance faible, via `socioeconomic_summary`)
- Rapport annuel Direction de la stratégie : Chiffres clés publiés dans le rapport annuel. (dépendance forte, via `socioeconomic_summary`)
