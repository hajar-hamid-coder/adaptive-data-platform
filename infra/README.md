# Infra locale : observabilité (Prometheus + Grafana) — P6-04

Dashboards et datasource Grafana **en code** (provisionnés au démarrage), métriques scrapées
depuis l'API ADP. Tout est lié à `127.0.0.1`.

## Contenu

| Chemin | Rôle |
|---|---|
| `prometheus/prometheus.yml` | scrape de l'API (`host.docker.internal:8000/metrics`, 15 s, Bearer) |
| `grafana/provisioning/datasources/prometheus.yml` | datasource Prometheus (uid `prometheus`) |
| `grafana/provisioning/dashboards/dashboards.yml` | provider fichier (dossier « ADP ») |
| `grafana/dashboards/adp-overview.json` | dashboard **ADP Overview** (uid `adp-overview`, 6 h, refresh 30 s) |
| `secrets/` | clé de scrape locale, git-ignorée : à créer avec `scripts/init_scrape_key.sh` (voir « Sans clé de scrape ») |

## Démarrage

```bash
# 1. clé de scrape éphémère de démo + digest pour l'API (une fois)
scripts/init_scrape_key.sh

# 2. pile : minio, postgres, prometheus, grafana (sans supprimer de volume)
docker compose up -d --wait

# 3. API sur l'hôte, avec le digest affiché par le script
export ADP_API_KEYS='prometheus:scraper:sha256:<digest>'  # valeur affichée par le script
#   (ajouter par ex. ,alice:viewer:sha256:<hex> : voir docs/securite.md)
uv run adp api serve --port 8000
```

## Ports et accès

| Service | URL | Accès |
|---|---|---|
| Grafana | http://127.0.0.1:3000 (`GRAFANA_PORT`) | anonyme, rôle **Viewer** (lecture seule) |
| Prometheus | http://127.0.0.1:9090 (`PROMETHEUS_PORT`) | lecture, cibles : `/targets` |
| API ADP | http://127.0.0.1:8000 | `/metrics` protégé par clé (`X-API-Key` ou `Bearer`), rôle `scraper` |
| Proxy TLS Caddy | https://localhost:8443 (`CADDY_HTTPS_PORT`) | HTTPS devant l'API, voir docs/securite.md |

Dashboard : Grafana → dossier **ADP** → **ADP Overview**. Filtre « Source » en haut.

## Choix de sécurité

- **Grafana sans compte administrateur** : `GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION=true`.
  Aucun mot de passe par défaut n'existe, donc rien à commiter ni à deviner. Accès anonyme
  Viewer, formulaire de connexion désactivé, inscription et édition désactivées, analytics et
  vérification de mises à jour coupées. Les dashboards sont figés : `allowUiUpdates: false`.
  Pour éditer, modifier le JSON versionné puis redémarrer Grafana.
- **Liaison locale** : tous les ports publiés sont `127.0.0.1:…`. Toute personne ayant accès à
  la machine peut lire les dashboards (choix démo, à revoir avant exposition).
- **Clé de scrape** : fichier `infra/secrets/adp_scrape_key` monté en lecture seule sur
  `/run/secrets`. Ce n'est pas la clé de l'utilisateur, mais une clé dédiée et éphémère ; l'API
  ne stocke que son digest. Rien de secret n'est écrit dans le dépôt.
- **Images épinglées** : `prom/prometheus:v3.13.5`, `grafana/grafana:13.2.5` (pas de `latest`).

## Sans clé de scrape

La pile démarre quand même. Sans `infra/secrets/adp_scrape_key`, la cible `adp-api` apparaît
**down** dans Prometheus, et le dashboard reste vide. Lancer `scripts/init_scrape_key.sh` puis
redémarrer Prometheus : `docker compose restart prometheus`.

## Alimenter les métriques

Les métriques de run sont lues dans le journal d'audit à chaque scrape : il faut qu'un run ait
été joué avec `ADP_REGISTRY_URL` pointant sur la même base que l'API.

1. Pointer l'API et le pipeline sur la même base (`ADP_REGISTRY_URL`).
2. Jouer un run (Dagster ou démo) : chaque étape écrit une ligne dans `run_audit` / `step_audit`.
3. Attendre un scrape (15 s) puis ouvrir le dashboard.

`adp_api_requests_total` ne vit que dans le processus API : il part de zéro au redémarrage.

## Limites connues

- Sous Linux, `host.docker.internal` pointe vers la passerelle Docker ; si l'API n'écoute que
  sur `127.0.0.1`, la cible ne répondra pas. Lancer l'API sur l'interface du bridge Docker
  (`--host`) ou utiliser Docker Desktop. Testé uniquement sous Docker Desktop macOS.
- Les quantiles de durée sont cumulés depuis le début du journal (l'histogramme est recalculé
  à chaque scrape à partir de la base), pas glissants.
- Le service `minio` utilise l'image `bitnamilegacy/minio:latest` (héritée, hors P6-04) ; elle
  n'est pas épinglée.
