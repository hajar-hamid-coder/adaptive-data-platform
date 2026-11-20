#!/usr/bin/env bash
# Crée la clé de scrape Prometheus de la démo (P6-04), idempotent.
#  - écrit infra/secrets/adp_scrape_key seulement s'il est absent (32 octets, droits 600) ;
#  - n'affiche JAMAIS la clé : seulement le digest SHA-256 à donner à l'API (ADP_API_KEYS).
# Usage : scripts/init_scrape_key.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="${ROOT}/infra/secrets"
KEY="${DIR}/adp_scrape_key"

mkdir -p "${DIR}"
chmod 700 "${DIR}"

if [[ ! -s "${KEY}" ]]; then
  umask 077
  python3 -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(32))' > "${KEY}"
  echo "Clé de scrape créée : ${KEY}" >&2
else
  echo "Clé de scrape existante conservée : ${KEY}" >&2
fi
chmod 600 "${KEY}"

DIGEST="$(python3 -c 'import hashlib, sys
with open(sys.argv[1], "rb") as f:
    print("prometheus:scraper:sha256:" + hashlib.sha256(f.read().strip()).hexdigest())' "${KEY}")"

echo "Pour l'API (hôte) :" >&2
echo "export ADP_API_KEYS='${DIGEST}'"
