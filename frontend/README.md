# Dashboard ADP (P5-06, animations P5-07)

React 18 + Vite + TypeScript strict + Tailwind 3 + TanStack Query + React Router. Design system : `DESIGN.md` ; tokens : `src/styles/tokens.css` (copie identique de `mockups/tokens.css`, vérifiée par un test). Quatre écrans : Pipeline, Qualité, Schémas, Revue du mapping. Aucune police ni script externe. Animations sobres avec Framer Motion (`src/motion.tsx`), désactivées sous `prefers-reduced-motion` (voir `DESIGN.md` §2.4).

## Démarrer en local avec les données de démo

```bash
# 1. données SYNTHÉTIQUES (idempotent, --reset pour reconstruire) ; affiche les variables à exporter
uv run python scripts/demo_data.py
# 2. exporter les variables ADP_* affichées, puis lancer l'API (terminal 1)
uv run adp api serve --port 8000
# 3. dashboard (terminal 2) ; ADP_API_KEY = clé de démo affichée à l'étape 1
export ADP_API_KEY='...'
cd frontend && npm ci && npm run dev      # http://127.0.0.1:5173
```

La clé de démo est tirée au hasard à chaque exécution du script, affichée une fois, jamais écrite ni commitée. L'API reçoit son empreinte SHA-256 (`ADP_API_KEYS=sha256:...`).

## Sécurité de la clé d'API

- Le code navigateur appelle `/api/...` (base configurable : `VITE_API_BASE`, non secrète) et n'envoie **jamais** de clé. Aucune variable `VITE_*` ne porte de secret.
- **Dev** : le proxy de `vite.config.ts` réécrit `/api/*` vers `ADP_API_URL` (défaut `http://127.0.0.1:8000`) et ajoute l'en-tête `X-API-Key` lu dans `ADP_API_KEY`, variable d'environnement du process Vite (voir `.env.example`, factice ; ne jamais committer de `.env`).
- **Production** : `npm run build` produit `dist/` statique. Le reverse proxy (nginx, Caddy, Traefik...) sert `dist/`, route `/api/` vers l'API en retirant le préfixe et injecte l'en-tête `X-API-Key` côté serveur, par exemple `proxy_set_header X-API-Key $adp_api_key;` avec la clé lue d'un secret du déploiement. Les utilisateurs sont authentifiés par le proxy (SSO ou réseau privé) : sans cela, quiconque atteint `/api` hérite de la clé.
- Seul le thème (`adp-theme`) est stocké dans `localStorage`.

## Limites assumées

- **Revue du mapping consultative (H-33)** : l'API n'expose aucune route de file de revue ; l'écran affiche un état « indisponible » et renvoie vers `adp review list/approve/reject`. Aucun endpoint d'écriture n'est ajouté.
- L'API ne liste pas les sources ni ne persiste de journal de run : les sources sont déduites de `/runs`, et les étapes Bronze/Silver/Gold ainsi que le statut (promu / quarantaine partielle) sont déduits du score du dernier run. Une source dont le seul lot est bloqué par la porte de schéma n'apparaît pas tant qu'elle n'a pas de score.
- Catalogue et lignage (`/catalog`) : types prêts (`api/types.ts`), écran prévu en P5-07 (animations comprises).

## Commandes

`npm run dev` · `npm run build` (typecheck puis build) · `npm test` (Vitest + Testing Library, `fetch` simulé) · `npm run typecheck`
