# Leçons apprises

Format : `YYYY-MM-DD — piège — correction`. (Vide au démarrage.)
- minio/minio n'est plus publiée sur Docker Hub : bitnamilegacy/minio utilisée (P0-02). À réévaluer avant la release.
- python3 système n'a pas openpyxl depuis l'ajout de .python-version : lancer scripts/backlog.py via 'uv run python'.
- Mots de passe de dev par défaut alignés sur change-me-locally (compose, config, healthcheck) : le healthcheck ne lit pas .env, donc les défauts doivent coïncider (revue P0).
