# Leçons apprises

Format : `YYYY-MM-DD — piège — correction`. (Vide au démarrage.)
- minio/minio n'est plus publiée sur Docker Hub : bitnamilegacy/minio utilisée (P0-02). À réévaluer avant la release.
- python3 système n'a pas openpyxl depuis l'ajout de .python-version : lancer scripts/backlog.py via 'uv run python'.
- Mots de passe de dev par défaut alignés sur change-me-locally (compose, config, healthcheck) : le healthcheck ne lit pas .env, donc les défauts doivent coïncider (revue P0).
- Publier un workflow GitHub Actions : le jeton doit avoir le scope `workflow` (classic) ou la permission Workflows (fine-grained) ; le trousseau macOS garde l'ancien jeton (`git credential-osxkeychain erase`).
- Le hook guard_bash donne de faux positifs sur le mot « force » suivi de « push » dans le texte d'une commande, et sur une commande mêlant publication git et suppression : écrire les notes avec Write/Edit et séparer les commandes.
