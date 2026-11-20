# Observabilité : audit des runs et métriques Prometheus (P6-01)

## Journal d'audit

Tables `run_audit` (un run par `(run_id, source)`) et `step_audit` (une ligne par étape :
`bronze`, `schema_check`, `silver`, `promote`), migration Alembic `0005`. Le run est **dérivé de
ses étapes** dans la même transaction (statut `failed` > `blocked` > `quarantined` > `success`).
Écriture idempotente : rejouer le même résultat ne fait rien, un résultat différent pour la même
clé lève `AuditError`. Seuls des compteurs, identifiants, énumérations, codes de raison et noms de
types d'exception sont stockés (jamais de valeur de donnée, de message brut ni de secret).
Un échec d'audit n'interrompt jamais un run de données (avertissement structuré `audit_failed`).

Lecture seule via l'API (clé requise, paginée) : `GET /audit/runs[?source=]` et
`GET /audit/runs/{run_id}/steps`. `/runs` (scores qualité) est inchangé : forme consommée par le
front. Cela ferme l'hypothèse H-34 (journal de runs persistant).

## Métriques (`GET /metrics`, format texte Prometheus)

| Métrique | Type | Labels |
|---|---|---|
| `adp_runs_total` | counter | `source`, `status` |
| `adp_step_duration_seconds` | histogramme | `step` |
| `adp_quality_score` | gauge (dernier run) | `source` |
| `adp_schema_changes_total` | counter | `source`, `impact` (compatible, review, blocking) |
| `adp_quarantined_rows_total` | counter | `source` |
| `adp_batches_promoted_total` | counter | `source` |
| `adp_api_requests_total` | counter | `route` (gabarit), `method`, `status` |

Jamais de `run_id`, de valeur de donnée ni de chemin brut en label (cardinalité bornée ; route
sans correspondance = `unmatched`, méthode inconnue = `OTHER`). Les métriques de run sont lues
dans les tables d'audit à chaque scrape : le pipeline (Dagster) et l'API sont deux processus,
la base est la vérité partagée et survit aux redémarrages. Seul le compteur de requêtes API vit
dans le processus API. Registre dédié (pas le registre global).

## Authentification et scrape

`/metrics` utilise les mêmes clés API que le reste (`ADP_API_KEYS`), via `X-API-Key` ou
`Authorization: Bearer <clé>` (ce que Prometheus envoie nativement). `/health` reste public.

```yaml
# prometheus.yml
scrape_configs:
  - job_name: adp-api
    metrics_path: /metrics
    scrape_interval: 30s
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/adp_api_key   # la clé brute, hors dépôt
    static_configs:
      - targets: ["adp-api:8000"]
```
