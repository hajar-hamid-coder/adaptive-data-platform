#!/usr/bin/env bash
# Installe l'outillage de développement hors du projet et régénère le graphe de connaissance.
# Idempotent : relançable sans effet de bord (réinstallation évitée si graphify est déjà présent).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# graphify est un outil global (uv tool) : il n'entre pas dans pyproject.toml ni uv.lock.
if ! command -v graphify >/dev/null 2>&1; then
    uv tool install graphifyy
fi

# Extraction AST locale (aucun appel LLM, aucune clé API requise), puis clustering sans
# nommage LLM et sans HTML : produit graphify-out/GRAPH_REPORT.md et graphify-out/graph.json.
graphify extract . --code-only
graphify cluster-only . --no-label --no-viz

echo "Graphe régénéré : $REPO_ROOT/graphify-out/GRAPH_REPORT.md"
