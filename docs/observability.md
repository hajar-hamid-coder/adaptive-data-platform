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
| `adp_compaction_files_before` | gauge (dernière passe) | `source`, `layer` (bronze, silver) |
| `adp_compaction_files_after` | gauge (dernière passe) | `source`, `layer` |
| `adp_compaction_bytes_before` | gauge (dernière passe) | `source`, `layer` |
| `adp_compaction_bytes_after` | gauge (dernière passe) | `source`, `layer` |
| `adp_api_requests_total` | counter | `route` (gabarit), `method`, `status` |

Jamais de `run_id`, de valeur de donnée ni de chemin brut en label (cardinalité bornée ; route
sans correspondance = `unmatched`, méthode inconnue = `OTHER`). Les métriques de run sont lues
dans les tables d'audit à chaque scrape : le pipeline (Dagster) et l'API sont deux processus,
la base est la vérité partagée et survit aux redémarrages. Seul le compteur de requêtes API vit
dans le processus API. Registre dédié (pas le registre global).

## Authentification et scrape

`/metrics` utilise le même mécanisme de clés que le reste (`ADP_API_KEYS`, capacité `metrics:read`,
voir docs/securite.md), via `X-API-Key` ou `Authorization: Bearer <clé>` (ce que Prometheus envoie
nativement). Une clé de rôle `scraper` ne lit que `/metrics` : à préférer pour Prometheus. `/health` reste public.

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

## Alerting et analyse de cause racine (P6-02)

`src/adp/observability/alerts.py` et `rca.py`. Après chaque étape auditée (`@audited_asset`),
`notify_run` évalue le run : **no-op tant que `ADP_ALERT_WEBHOOK_URL` n'est pas défini**, et un
problème d'alerte ne change jamais l'issue du run.

- **Seuils** (`config/alerts.yaml`) : `run_failed`, `schema_blocked`, `quarantine_rate`,
  `quality_score_below`, `sla_freshness` (`freshness_alerts`, à appeler par une planification).
- **Anti-bruit** : clé de dédup = hash (source, type, motif de codes) ; table `alert_state`
  (Alembic `0006`) ; même alerte supprimée pendant `dedup_window_minutes` (« vu N fois » au
  renvoi) ; un même run réévalué ne compte pas ; regroupement en un message par run ; heures
  calmes (UTC) ; message de rétablissement quand la source repasse au vert. Décision atomique
  (INSERT unique ou UPDATE conditionnel) : sûre en concurrence. Livraison « au plus une fois ».
- **Webhook** : URL uniquement par variable d'environnement (secret, jamais loguée) ; https,
  port par défaut, sans identifiants, hôte dans l'allowlist (`hooks.slack.com`, `discord.com`,
  `discordapp.com`) et chemin attendu, sinon refus (anti-SSRF) ; pas de redirection suivie ;
  timeout 5 s, 3 tentatives max (erreurs réseau, 429, 5xx).
- **Assainissement** : mentions neutralisées (`@everyone`, `@here`, `<@id>`), liens retirés,
  balisage supprimé, longueur bornée ; Slack en `plain_text`, Discord `allowed_mentions` vide.
- **RCA** : contexte minimal (étapes, codes, type d'exception, changements de schéma réels du
  registre : noms de colonnes et jetons de type/format uniquement), hypothèses classées citant
  des références du contexte (`schema:drop_column:taux_activite`). Par défaut, règles locales
  déterministes ; LLM en option (`rca.use_llm`, clé `ANTHROPIC_API_KEY`) avec garde de
  confidentialité, budget, cache ; une hypothèse LLM sans preuve réelle est rejetée.
- **Test** : `uv run adp alerts test --dry-run [--kind discord]` affiche le payload sans envoyer.

## Auto-optimisation guidée par l'usage (P6-05)

- **Journal d'usage** (`src/adp/optimizer/usage.py`, `config/optimizer.yaml`) : une ligne JSONL
  par requête analytique (`/products/{name}/data`, `/indicators/{name}`) avec le gabarit de route,
  la cible, la *forme* de la requête (opération, colonnes de filtre/tri/agrégat), la durée, les
  comptes de lignes, la taille de page et une *tranche* d'offset. Jamais de valeur de paramètre,
  de chemin brut, d'en-tête ni de ligne. Échantillonnage, rotation et plafond de rétention
  configurables ; une erreur de journal ne casse jamais la requête.
- **Advisor** (`advisor.py`) : règles pures (preuve minimale, p95, part du temps total, gain
  attendu) ; types émis : vue matérialisée d'indicateur, pagination par position (keyset).
- **Application sous validation humaine** : `adp optimize recommend`, `adp optimize apply ID
  --by X --reason R`, `adp optimize revert ID --by X --reason R`, `adp optimize report ID`.
  Artefacts additifs sous `gold_opt/` (Gold/Silver jamais modifiés), journal append-only
  `gold_opt/_journal/`, artefact périmé ignoré (repli sur la source), équivalence vérifiée avant
  activation, coupe-circuit `ADP_OPTIMIZER_DISABLED=1`.
- **Mesure** : `make bench-optim` (1 M lignes synthétiques, hors CI) écrit `docs/optimisation.md`.
