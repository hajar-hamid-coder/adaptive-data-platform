# Sécurité : RBAC, TLS local, scans (P6-07)

## Rôles et clés

Rôles : `viewer` < `steward` < `admin`, plus `scraper` (Prometheus, hors hiérarchie : il ne lit que
`/metrics`). Les droits sont des **capacités** explicites (`src/adp/api/rbac.py`, table
`CAPABILITY_ROLES`), jamais une comparaison de chaînes dans une route.

`ADP_API_KEYS` (séparées par des virgules, aucun secret dans le repo) :

| Entrée | Rôle | Principal |
|---|---|---|
| `<clé brute>` ou `sha256:<hex>` (ancien format) | `viewer` (moindre privilège) | `key-<n>` |
| `nom:rôle:<clé brute>` | celui indiqué | `nom` |
| `nom:rôle:sha256:<hex>` (recommandé) | celui indiqué | `nom` |

`nom` : `[A-Za-z0-9._-]`, 64 max. Une entrée mal formée (rôle inconnu, nom invalide, empreinte non
hexadécimale de 64 caractères) est **ignorée** avec un log `api_key_entry_ignored` qui ne porte que
sa position : échec fermé, aucun secret journalisé. Une clé brute contenant `:` doit passer par la
forme `sha256:`. Empreinte : `printf %s "$CLE" | shasum -a 256`.

```bash
export ADP_API_KEYS='prometheus:scraper:sha256:<hex>,alice:steward:sha256:<hex>,bob:viewer:sha256:<hex>'
```

Réponses : `401 {"detail":"Unauthorized"}` sans clé ou clé inconnue ; `403 {"detail":"Forbidden"}`
si le rôle est insuffisant (corps générique, sans lister les capacités). Les refus sont journalisés
(`api_auth_denied` : statut, capacité, chemin, nom du principal s'il est connu, jamais la clé). La
comparaison reste à temps constant sur toutes les clés configurées. Le principal authentifié
(nom, rôle) est disponible via `request.state.principal` ; c'est lui, et jamais le corps de la
requête, qui alimente le champ `by` du journal de décisions.

## Matrice rôles × endpoints

| Endpoint | Capacité | viewer | steward | admin | scraper |
|---|---|---|---|---|---|
| `GET /health` | public | oui | oui | oui | oui |
| `GET /products`, `/indicators`, `/schemas/*`, `/quality/*`, `/runs`, `/catalog*` | `data:read` | oui | oui | oui | non |
| `GET /review` | `review:read` | oui | oui | oui | non |
| `POST /review/{id}/approve`, `/reject` | `review:decide` | non | oui | oui | non |
| `GET /audit/*` (journal d'exploitation) | `audit:read` | non | oui | oui | non |
| `GET /metrics` (clé ou Bearer) | `metrics:read` | oui | oui | oui | oui |

Choix : `audit` est réservé à steward+ (journal d'exploitation : étapes, erreurs, volumes) ; `scraper`
existe pour que la clé stockée dans Prometheus ne donne pas accès aux données. Le test
`tests/api/test_rbac.py` parcourt les chemins OpenAPI : toute route non publique sans garde (401
sans clé) ou accessible au `scraper` hors `/metrics` fait échouer la suite (anti-oubli).

### Revue de mapping par l'API

Seule écriture de l'API (lève partiellement l'hypothèse H-33) : `POST /review/{id}/approve|reject`,
qui appellent `ReviewQueue.approve/reject` comme la CLI `adp review` (mêmes garde-fous, journal
append-only). Corps optionnel `{"note": "..."}` (500 caractères max, tout autre champ refusé). Un
rejeu de la même décision renvoie l'élément inchangé (200, pas de nouvelle ligne de journal) ; la
décision inverse sur un élément déjà décidé répond 409 ; élément inconnu : 404. Les réponses ne
contiennent ni justification LLM ni valeur de donnée.

## TLS en local (Caddy)

`docker compose up -d --wait caddy` démarre Caddy (image `caddy:2.10.2-alpine`) qui termine le TLS
devant l'API lancée sur l'hôte (`uv run adp api serve --port 8000`, joint via
`host.docker.internal:8000`). Config : `infra/caddy/Caddyfile`. Ports liés à `127.0.0.1` :
`https://localhost:8443` (`CADDY_HTTPS_PORT`) et `http://localhost:8080` (`CADDY_HTTP_PORT`, redirige
en 308 vers HTTPS). Certificat : `tls internal`, CA locale générée dans le volume `caddy-data` ;
aucun certificat ni clé dans le repo. En-têtes ajoutés : HSTS, `X-Content-Type-Options`,
`X-Frame-Options: DENY`, CSP minimale, `Referrer-Policy: no-referrer`. Le journal d'accès retire
`X-Api-Key`, `Authorization` et `Cookie`. Caddy pose `X-Forwarded-For/Proto/Host`.

Faire confiance à la CA locale :

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
curl --cacert ./caddy-root.crt https://localhost:8443/health
curl --cacert ./caddy-root.crt -H "X-API-Key: $CLE" https://localhost:8443/products
```

Ne pas importer cette CA dans le magasin système sauf besoin (elle peut signer pour n'importe quel
nom) ; `--cacert` suffit. La supprimer = supprimer le volume `caddy-data` (nouvelle CA au prochain
démarrage). Limite : `Strict-Transport-Security` sur `localhost` est sans portée réelle hors
navigateur ; l'API en HTTP clair sur `127.0.0.1:8000` reste joignable depuis l'hôte (en production,
ne l'exposer que derrière le proxy).

## Scans automatiques (CI, job `security`)

- `pip-audit` sur le lock exporté (`uv export --frozen --no-hashes --no-emit-project`) ;
- `npm audit --audit-level=high` sur `frontend/package-lock.json` ;
- gitleaks (action épinglée sur un SHA) avec `.gitleaks.toml` : règles par défaut, allowlist des
  valeurs factices de `.env.example`, des fixtures `tests/` et des empreintes `sha256:`.

En local : `uv export --frozen --no-hashes --no-emit-project > /tmp/req.txt && uvx pip-audit -r
/tmp/req.txt --no-deps --disable-pip`, `cd frontend && npm audit`, `gitleaks detect -c .gitleaks.toml`
(binaire à installer séparément).
