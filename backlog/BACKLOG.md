# Backlog — Plateforme de Données Intelligente et Évolutive

> Fichier généré depuis `backlog.xlsx` (source de vérité). Ne pas éditer : `python scripts/backlog.py export-md`.

## P0 — Fondations & environnement Claude Code

*Repo propre, outillage, CI, Docker local, graphify, ADR d'architecture.* — gate utilisateur : **Non**

| Ordre | ID | Tâche | Modèle | Dépend. | Statut |
|---|---|---|---|---|---|
| 1 | P0-01 | Structure du repo & outillage Python | Haiku | — | Non fait |
| 2 | P0-02 | Docker Compose local (MinIO, Postgres) | Sonnet | P0-01 | Non fait |
| 3 | P0-03 | Configuration & gestion des secrets | Sonnet | P0-01 | Non fait |
| 4 | P0-04 | Logging structuré & modèle d'erreurs commun | Sonnet | P0-01 | Non fait |
| 5 | P0-05 | CI GitHub Actions | Sonnet | P0-01 | Non fait |
| 6 | P0-06 | Installer graphify et cartographier le repo | Haiku | P0-01 | Non fait |
| 7 | P0-07 | ADR d'architecture fondatrices | Opus | P0-02 | Non fait |

### P0-01 — Structure du repo & outillage Python
- **Objectif** : Créer l'ossature du projet (src layout), gestion des dépendances et qualité de code.
- **Code** : pyproject.toml (uv, ruff, mypy, pytest, openpyxl, duckdb, pydantic), src/adp/__init__.py, tests/test_smoke.py, Makefile (install, lint, test, demo), .pre-commit-config.yaml.
- **Pourquoi** : Un repo reproductible en une commande est le premier signal de professionnalisme. Compétence : packaging Python moderne, tooling qualité.
- **Test** : `uv sync && make lint && make test` → ruff sans erreur, 1 test smoke vert.
- **Hypothèse** : H-06 · **Skills** : ponytail

### P0-02 — Docker Compose local (MinIO, Postgres)
- **Objectif** : Environnement local reproductible : stockage objet S3 et base de métadonnées.
- **Code** : docker-compose.yml (minio, postgres, minio-init créant les buckets bronze/silver/gold), scripts/healthcheck.py, .env.example.
- **Pourquoi** : Simule un data lake S3 localement sans coût cloud. Compétence : conteneurisation, infra as code.
- **Test** : `docker compose up -d && python scripts/healthcheck.py` → 3 buckets présents et Postgres répond (exit 0).
- **Hypothèse** : H-04 · **Skills** : ponytail

### P0-03 — Configuration & gestion des secrets
- **Objectif** : Centraliser la config typée et garantir qu'aucun secret n'est commité.
- **Code** : src/adp/config.py (pydantic-settings), .env.example complet, tests/test_config.py, hook pre-commit de détection de secrets.
- **Pourquoi** : Séparation config/code (12-factor) et hygiène sécurité. Compétence : sécurité applicative, DevSecOps.
- **Test** : `pytest tests/test_config.py` vert ; ajouter une fausse clé AKIA… dans un fichier → le pre-commit refuse le commit.
- **Hypothèse** : — · **Skills** : ponytail

### P0-04 — Logging structuré & modèle d'erreurs commun
- **Objectif** : Logs JSON corrélés par run_id et hiérarchie d'exceptions du domaine.
- **Code** : src/adp/logging.py (structlog), src/adp/errors.py (AdpError, SchemaError, QualityError, IngestionError), tests/test_logging.py.
- **Pourquoi** : Base de l'observabilité (P6) : tout événement est traçable. Compétence : observabilité, design d'erreurs.
- **Test** : `pytest tests/test_logging.py` → chaque ligne de log est du JSON valide contenant run_id, level, event.
- **Hypothèse** : — · **Skills** : ponytail

### P0-05 — CI GitHub Actions
- **Objectif** : Pipeline CI : lint, typage, tests, sur chaque push/PR.
- **Code** : .github/workflows/ci.yml (matrix Python 3.12, cache uv), badge dans README.
- **Pourquoi** : Démontre l'intégration continue. Compétence : CI/CD.
- **Test** : Push d'une branche → workflow vert dans l'onglet Actions ; casser volontairement un test → workflow rouge.
- **Hypothèse** : — · **Skills** : ponytail

### P0-06 — Installer graphify et cartographier le repo
- **Objectif** : Générer le graphe de connaissance du projet pour réduire les tokens de contexte.
- **Code** : scripts/setup_tools.sh (pip install graphifyy && graphify install), graphify-out/ (GRAPH_REPORT.md, graph.json), règle dans CLAUDE.md pour lire GRAPH_REPORT.md avant d'explorer.
- **Pourquoi** : Graphe de code interrogeable = navigation rapide et moins coûteuse. Compétence : outillage agentique, knowledge graph.
- **Test** : `graphify .` produit GRAPH_REPORT.md non vide ; `git status` montre graph.json ignoré ou commité selon la décision H-07.
- **Hypothèse** : H-07 · **Skills** : graphify

### P0-07 — ADR d'architecture fondatrices
- **Objectif** : Documenter les décisions structurantes : médaillon, stockage, metadata store, orchestrateur, politique LLM.
- **Code** : docs/adr/0001-medallion.md … 0006-llm-policy.md (contexte/décision/conséquences), docs/architecture.md avec diagramme Mermaid.
- **Pourquoi** : Les ADR montrent le raisonnement d'un architecte, pas seulement du code. Compétence : architecture de données.
- **Test** : `pytest tests/test_docs.py` : chaque ADR contient les 3 sections ; le diagramme Mermaid est rendu sur GitHub.
- **Hypothèse** : H-03, H-04 · **Skills** : ponytail, graphify

## P1 — Ingestion & couche Bronze

*Connecteurs multi-sources, écriture Bronze immuable, rejets, simulateur de dérive.* — gate utilisateur : **Non**

| Ordre | ID | Tâche | Modèle | Dépend. | Statut |
|---|---|---|---|---|---|
| 8 | P1-01 | Contrat de source & registry de sources | Sonnet | P0-04 | Non fait |
| 9 | P1-02 | Connecteur fichiers (CSV/Excel/JSON) | Sonnet | P1-01 | Non fait |
| 10 | P1-03 | Connecteur REST API | Sonnet | P1-01 | Non fait |
| 11 | P1-04 | Connecteur base de données (incrémental) | Sonnet | P1-01 | Non fait |
| 12 | P1-05 | Écriture Bronze immuable | Sonnet | P1-02, P0-02 | Non fait |
| 13 | P1-06 | Gestion des erreurs d'ingestion & dead-letter | Sonnet | P1-05 | Non fait |
| 14 | P1-07 | Simulateur de dérive de schéma | Sonnet | P1-02 | Non fait |
| 15 | P1-08 | Archivage & cycle de vie Bronze | Sonnet | P1-05 | Non fait |
| 16 | P1-09 | Assets & sensors Dagster d'ingestion | Sonnet | P1-05, P1-06 | Non fait |

### P1-01 — Contrat de source & registry de sources
- **Objectif** : Décrire chaque source de façon déclarative.
- **Code** : src/adp/sources/spec.py (SourceSpec pydantic : type, uri, auth ref, format, fréquence, clé), config/sources/*.yaml, loader + validation.
- **Pourquoi** : Le contrat de source rend l'ajout d'une source déclaratif. Compétence : data contracts.
- **Test** : `pytest tests/sources/test_spec.py` : un YAML valide charge, un YAML invalide lève SourceSpecError avec message clair.
- **Hypothèse** : H-01 · **Skills** : ponytail

### P1-02 — Connecteur fichiers (CSV/Excel/JSON)
- **Objectif** : Lire des fichiers hétérogènes avec détection auto d'encodage, délimiteur et feuille.
- **Code** : src/adp/connectors/files.py (charset-normalizer, csv.Sniffer, openpyxl), fixtures dans tests/data/.
- **Pourquoi** : Les fichiers sales sont la réalité du terrain. Compétence : ingestion robuste.
- **Test** : `pytest tests/connectors/test_files.py` : CSV latin-1 `;`, CSV utf-8 `,`, XLSX multi-feuilles, JSON lines → DataFrame correct.
- **Hypothèse** : — · **Skills** : ponytail

### P1-03 — Connecteur REST API
- **Objectif** : Ingestion d'API paginées avec retry, backoff et limite de débit.
- **Code** : src/adp/connectors/rest.py (httpx, tenacity), pagination page/offset/cursor, tests avec respx.
- **Pourquoi** : Résilience réseau et pagination. Compétence : API ingestion.
- **Test** : `pytest tests/connectors/test_rest.py` : 3 pages agrégées, 2 erreurs 503 puis succès (retry), 429 respecte Retry-After.
- **Hypothèse** : H-01 · **Skills** : ponytail

### P1-04 — Connecteur base de données (incrémental)
- **Objectif** : Extraction SQL avec watermark pour ne lire que le nouveau.
- **Code** : src/adp/connectors/sql.py (SQLAlchemy), table de watermarks, tests sur SQLite et Postgres de test.
- **Pourquoi** : Chargement incrémental = coût et fraîcheur maîtrisés. Compétence : CDC léger.
- **Test** : `pytest tests/connectors/test_sql.py` : 2e exécution sans nouvelle ligne → 0 ligne lue ; après insert de 5 lignes → 5 lues.
- **Hypothèse** : — · **Skills** : ponytail

### P1-05 — Écriture Bronze immuable
- **Objectif** : Persister le brut en Parquet sur MinIO avec métadonnées et idempotence.
- **Code** : src/adp/bronze/writer.py : partition source/ingestion_date, colonnes techniques (_run_id, _ingested_at, _source, _row_hash), manifeste JSON, écriture atomique.
- **Pourquoi** : Immuabilité + traçabilité = auditabilité. Compétence : lakehouse, idempotence.
- **Test** : `pytest tests/bronze/test_writer.py` : rejouer le même lot ne crée pas de doublon ; le manifeste contient hash et nb lignes ; fichier jamais modifié après écriture.
- **Hypothèse** : — · **Skills** : ponytail

### P1-06 — Gestion des erreurs d'ingestion & dead-letter
- **Objectif** : Ne jamais perdre une ligne : les rejets sont stockés avec leur raison.
- **Code** : src/adp/bronze/deadletter.py, schéma de rejet (raison, ligne brute, source, run_id), politique de retry.
- **Pourquoi** : Pas de perte silencieuse de données. Compétence : fiabilité des pipelines.
- **Test** : `pytest tests/bronze/test_deadletter.py` : fichier avec 3 lignes corrompues sur 100 → 97 en Bronze, 3 en dead-letter avec motif.
- **Hypothèse** : — · **Skills** : ponytail

### P1-07 — Simulateur de dérive de schéma
- **Objectif** : Générer des jeux v1→v2→v3 reproductibles (colonne ajoutée, renommée, type changé).
- **Code** : scripts/drift_simulator.py (seed fixe), tests/data/drift/{v1,v2,v3}/, README du scénario de démo.
- **Pourquoi** : Donne un banc d'essai déterministe pour P2/P3 et la démo. Compétence : data engineering de test.
- **Test** : `python scripts/drift_simulator.py --seed 42` deux fois → fichiers identiques (hash égal) ; diff v1/v2 documenté.
- **Hypothèse** : H-01 · **Skills** : ponytail

### P1-08 — Archivage & cycle de vie Bronze
- **Objectif** : Règles de rétention et d'archivage configurables.
- **Code** : src/adp/bronze/lifecycle.py, config/retention.yaml, commande CLI `adp bronze archive --dry-run`.
- **Pourquoi** : Gouvernance et coûts de stockage. Compétence : data lifecycle.
- **Test** : `pytest tests/bronze/test_lifecycle.py` : lots > rétention archivés, récents intacts ; --dry-run ne modifie rien.
- **Hypothèse** : — · **Skills** : ponytail

### P1-09 — Assets & sensors Dagster d'ingestion
- **Objectif** : Orchestrer l'ingestion avec Dagster (assets, partitions, sensors).
- **Code** : src/adp/orchestration/ingestion.py, definitions.py, dagster.yaml, test de matérialisation.
- **Pourquoi** : Orchestration déclarative avec lignage natif. Compétence : Dagster.
- **Test** : `dagster asset materialize -m adp.orchestration` → asset Bronze matérialisé ; `pytest tests/orchestration` vert.
- **Hypothèse** : H-08 · **Skills** : ponytail

## P2 — Détection de schéma & gestion des évolutions

*Registry versionné, diff de schémas, classification d'impact, politique d'évolution.* — gate utilisateur : **Non**

| Ordre | ID | Tâche | Modèle | Dépend. | Statut |
|---|---|---|---|---|---|
| 17 | P2-01 | Profilage & empreinte de schéma | Sonnet | P1-05 | Non fait |
| 18 | P2-02 | Schema registry versionné | Opus | P2-01, P0-02 | Non fait |
| 19 | P2-03 | Moteur de diff de schémas | Opus | P2-02, P1-07 | Non fait |
| 20 | P2-04 | Classification d'impact & politique d'évolution | Opus | P2-03 | Non fait |
| 21 | P2-05 | Rapport d'évolution & événements | Haiku | P2-04 | Non fait |
| 22 | P2-06 | Intégration à l'ingestion (détection automatique) | Sonnet | P2-04, P1-09 | Non fait |
| 23 | P2-07 | Tests de propriétés du diff | Sonnet | P2-03 | Non fait |

### P2-01 — Profilage & empreinte de schéma
- **Objectif** : Extraire pour chaque colonne : type inféré, nullabilité, cardinalité, stats, exemples.
- **Code** : src/adp/schema/profile.py (ColumnProfile, SchemaFingerprint), inférence de type robuste (dates, nombres FR/EN).
- **Pourquoi** : Le profil est l'entrée de la détection et du mapping. Compétence : data profiling.
- **Test** : `pytest tests/schema/test_profile.py` : '12,5' et '12.5' → float ; '2024-01-31' et '31/01/2024' → date ; empreinte stable pour mêmes données.
- **Hypothèse** : — · **Skills** : ponytail

### P2-02 — Schema registry versionné
- **Objectif** : Stocker chaque version de schéma par source avec hash et versioning sémantique.
- **Code** : src/adp/schema/registry.py (SQLAlchemy, tables schema_version, schema_change), migrations Alembic, API register/get/latest/history.
- **Pourquoi** : Mémoire des schémas = base de toute évolution gouvernée. Compétence : metadata management.
- **Test** : `pytest tests/schema/test_registry.py` : même schéma → même version (idempotent) ; changement → version+1 ; historique ordonné.
- **Hypothèse** : H-04 · **Skills** : ponytail, graphify

### P2-03 — Moteur de diff de schémas
- **Objectif** : Détecter ajout, suppression, changement de type et renommage probable (score de similarité).
- **Code** : src/adp/schema/diff.py (SchemaDiff, ChangeKind), heuristique de renommage (nom + type + distribution), seuils configurables.
- **Pourquoi** : Cœur différenciant : distinguer renommage vs suppression+ajout. Compétence : algorithmique de similarité.
- **Test** : `pytest tests/schema/test_diff.py` sur v1→v2→v3 du simulateur : renommage détecté avec score ≥ seuil, 0 faux positif sur 20 cas témoins.
- **Hypothèse** : H-03 · **Skills** : ponytail, graphify

### P2-04 — Classification d'impact & politique d'évolution
- **Objectif** : Classer chaque changement : compatible / à revoir / bloquant, et définir l'action automatique.
- **Code** : src/adp/schema/policy.py, config/evolution_policy.yaml (règles par ChangeKind), ChangeImpact enum.
- **Pourquoi** : Traduit la technique en risque métier (lien BIA). Compétence : gouvernance, analyse d'impact.
- **Test** : `pytest tests/schema/test_policy.py` : suppression de clé → BLOQUANT ; ajout de colonne nullable → COMPATIBLE ; widening int→float → COMPATIBLE.
- **Hypothèse** : H-02 · **Skills** : ponytail

### P2-05 — Rapport d'évolution & événements
- **Objectif** : Produire un rapport lisible et émettre un événement à chaque changement.
- **Code** : src/adp/schema/report.py (Markdown + JSON), bus d'événements interne (SchemaChanged).
- **Pourquoi** : Rendre le changement visible aux humains et aux systèmes. Compétence : communication data.
- **Test** : `pytest tests/schema/test_report.py` : rapport contient version avant/après, liste des changements, impact ; événement émis une seule fois.
- **Hypothèse** : — · **Skills** : ponytail

### P2-06 — Intégration à l'ingestion (détection automatique)
- **Objectif** : À chaque lot : profil → diff → politique ; bloquer ou continuer.
- **Code** : src/adp/orchestration/schema_gate.py, asset Dagster schema_check entre Bronze et Silver.
- **Pourquoi** : Automatisation bout en bout de la détection. Compétence : intégration de pipeline.
- **Test** : Run Dagster sur v1 puis v2 → v2 passe avec rapport ; v3 (breaking) → arrêt propre avec statut 'blocked' et motif.
- **Hypothèse** : — · **Skills** : ponytail

### P2-07 — Tests de propriétés du diff
- **Objectif** : Renforcer la fiabilité du diff avec du property-based testing.
- **Code** : tests/schema/test_diff_properties.py (hypothesis) : diff(a,a)=∅, symétrie des ajouts/suppressions, stabilité à l'ordre des colonnes.
- **Pourquoi** : Preuve de rigueur de test. Compétence : property-based testing.
- **Test** : `pytest tests/schema/test_diff_properties.py` : 500 exemples générés, 0 contre-exemple.
- **Hypothèse** : — · **Skills** : ponytail

## P3 — Auto-mapping adaptatif & couche Silver

*Mapping règles + LLM/RAG, revue humaine, transformations sûres, nettoyage, dédup, Silver.* — gate utilisateur : **Non**

| Ordre | ID | Tâche | Modèle | Dépend. | Statut |
|---|---|---|---|---|---|
| 24 | P3-01 | Modèle canonique & dictionnaire sémantique | Sonnet | P2-01 | Non fait |
| 25 | P3-02 | Mapping par règles & fuzzy | Sonnet | P3-01 | Non fait |
| 26 | P3-03 | Mapping assisté par LLM + RAG | Opus | P3-02 | Non fait |
| 27 | P3-04 | Politique de décision & revue humaine | Opus | P3-02, P3-03 | Non fait |
| 28 | P3-05 | Transformations de types sûres | Opus | P3-04 | Non fait |
| 29 | P3-06 | Nettoyage & normalisation | Sonnet | P3-05 | Non fait |
| 30 | P3-07 | Déduplication | Sonnet | P3-06 | Non fait |
| 31 | P3-08 | Écriture Silver & apprentissage du dictionnaire | Sonnet | P3-07 | Non fait |
| 32 | P3-09 | Évaluation du mapping (jeu annoté) | Sonnet | P3-03, P3-08 | Non fait |

### P3-01 — Modèle canonique & dictionnaire sémantique
- **Objectif** : Définir les champs cibles et leurs synonymes (FR/EN).
- **Code** : config/semantic/canonical.yaml (champ, type, description, synonymes, unité), src/adp/mapping/dictionary.py.
- **Pourquoi** : Le référentiel sémantique est ce qui rend le mapping intelligent. Compétence : modélisation sémantique, BA.
- **Test** : `pytest tests/mapping/test_dictionary.py` : chargement valide, doublon de synonyme détecté, recherche insensible à la casse/accents.
- **Hypothèse** : H-01 · **Skills** : ponytail

### P3-02 — Mapping par règles & fuzzy
- **Objectif** : Premier niveau déterministe : exact, normalisé, synonyme, fuzzy (rapidfuzz) avec score.
- **Code** : src/adp/mapping/rules.py (MappingCandidate{source_col,target,score,method}), normalisation (accents, casse, séparateurs).
- **Pourquoi** : Baseline explicable avant tout LLM. Compétence : NLP léger, baseline-first.
- **Test** : `pytest tests/mapping/test_rules.py` : 'N° Client' → customer_id (méthode normalisé) ; 'cust_nm' → customer_name (fuzzy ≥ seuil).
- **Hypothèse** : — · **Skills** : ponytail

### P3-03 — Mapping assisté par LLM + RAG
- **Objectif** : Proposer des mappings pour les colonnes non résolues via embeddings + LLM, sortie JSON validée.
- **Code** : src/adp/mapping/llm.py (interface LLMProvider, Claude via SDK anthropic, fallback local), embeddings du glossaire, prompt structuré, validation pydantic, cache disque.
- **Pourquoi** : Le LLM propose, ne décide pas. Compétence : LLM/RAG appliqué, prompt engineering, sorties structurées.
- **Test** : `pytest tests/mapping/test_llm.py` avec provider simulé : JSON invalide rejeté, réponse hors glossaire rejetée, cache évite 2e appel ; test d'intégration marqué `slow` avec vraie clé.
- **Hypothèse** : H-09 · **Skills** : ponytail, graphify

### P3-04 — Politique de décision & revue humaine
- **Objectif** : Auto-appliquer au-dessus d'un seuil, sinon file de revue (human-in-the-loop).
- **Code** : src/adp/mapping/decision.py, table review_queue, CLI `adp review list/approve/reject`, journalisation de chaque décision.
- **Pourquoi** : Contrôle du risque IA : traçabilité et réversibilité. Compétence : IA responsable, gouvernance.
- **Test** : `pytest tests/mapping/test_decision.py` : score 0.95 → auto ; 0.7 → file ; décision approuvée enregistrée avec auteur et horodatage.
- **Hypothèse** : H-02 · **Skills** : ponytail

### P3-05 — Transformations de types sûres
- **Objectif** : Appliquer conversions et règles de transformation sans exécution de code arbitraire.
- **Code** : src/adp/mapping/transform.py : DSL déclaratif (cast, parse_date, strip, map_values, unit_convert), évaluateur whitelisté, erreurs par ligne vers dead-letter.
- **Pourquoi** : Évite l'injection via sortie LLM. Compétence : sécurité, conception de DSL.
- **Test** : `pytest tests/mapping/test_transform.py` : transformation inconnue refusée ; tentative `__import__` dans une règle rejetée ; conversions correctes.
- **Hypothèse** : — · **Skills** : ponytail

### P3-06 — Nettoyage & normalisation
- **Objectif** : Standardiser texte, dates, nombres, unités et valeurs manquantes.
- **Code** : src/adp/silver/clean.py, règles par champ canonique, journal des corrections.
- **Pourquoi** : Qualité à la source de Silver. Compétence : data cleansing.
- **Test** : `pytest tests/silver/test_clean.py` : espaces/casse/accents normalisés, dates → ISO 8601, 'N/A','-','' → null.
- **Hypothèse** : — · **Skills** : ponytail

### P3-07 — Déduplication
- **Objectif** : Dédoublonnage exact et approximatif (blocking + similarité).
- **Code** : src/adp/silver/dedup.py (clés naturelles, blocking, rapidfuzz), règle de survivance, rapport de doublons.
- **Pourquoi** : Entity resolution de base. Compétence : data science appliquée.
- **Test** : `pytest tests/silver/test_dedup.py` : 10 doublons injectés sur 1000 lignes → 10 trouvés, 0 faux positif.
- **Hypothèse** : — · **Skills** : ponytail

### P3-08 — Écriture Silver & apprentissage du dictionnaire
- **Objectif** : Écrire Silver et enrichir automatiquement les synonymes avec les mappings validés.
- **Code** : src/adp/silver/writer.py (Parquet typé, versionné par schéma), src/adp/mapping/learn.py (ajout de synonymes validés, jamais non validés).
- **Pourquoi** : Boucle d'amélioration continue. Compétence : MLOps léger, feedback loop.
- **Test** : `pytest tests/silver/test_writer.py tests/mapping/test_learn.py` : mapping approuvé → synonyme ajouté ; au prochain run même colonne résolue par règle sans LLM.
- **Hypothèse** : — · **Skills** : ponytail

### P3-09 — Évaluation du mapping (jeu annoté)
- **Objectif** : Mesurer précision/rappel des mappings règles vs règles+LLM.
- **Code** : tests/data/gold_mappings.csv (≥100 colonnes annotées), src/adp/mapping/eval.py, rapport docs/eval/mapping.md avec matrice de confusion.
- **Pourquoi** : Une métrique chiffrée vaut mille promesses — argument fort en entretien. Compétence : évaluation de modèles.
- **Test** : `python -m adp.mapping.eval` → rapport généré ; précision règles+LLM ≥ précision règles seules (seuil à fixer, voir H-09).
- **Hypothèse** : H-09 · **Skills** : ponytail

## P4 — Qualité des données & quarantaine

*Règles qualité, anomalies, scores, quarantaine dynamique, validation Silver→Gold.* — gate utilisateur : **Non**

| Ordre | ID | Tâche | Modèle | Dépend. | Statut |
|---|---|---|---|---|---|
| 33 | P4-01 | Moteur de règles qualité | Sonnet | P3-08 | Non fait |
| 34 | P4-02 | Anomalies & dérive de distribution | Sonnet | P4-01 | Non fait |
| 35 | P4-03 | Score de qualité | Sonnet | P4-01 | Non fait |
| 36 | P4-04 | Quarantaine dynamique | Opus | P4-01, P4-03 | Non fait |
| 37 | P4-05 | Rapports de qualité | Haiku | P4-03 | Non fait |
| 38 | P4-06 | Contrôles métier & cohérence | Sonnet | P4-01 | Non fait |
| 39 | P4-07 | Validation finale Silver → Gold | Sonnet | P4-04, P4-06 | Non fait |

### P4-01 — Moteur de règles qualité
- **Objectif** : Règles déclaratives : not_null, unique, plage, regex, référentiel, fraîcheur.
- **Code** : src/adp/quality/rules.py, config/quality/*.yaml, résultats par règle (passé/échoué/lignes).
- **Pourquoi** : Qualité codée comme contrat. Compétence : data quality engineering.
- **Test** : `pytest tests/quality/test_rules.py` : chaque type de règle testé passant et échouant, avec nombre de lignes en échec correct.
- **Hypothèse** : — · **Skills** : ponytail

### P4-02 — Anomalies & dérive de distribution
- **Objectif** : Détecter valeurs aberrantes et dérive entre lots (z-score/IQR, PSI, KS).
- **Code** : src/adp/quality/anomaly.py, src/adp/quality/drift.py, seuils configurables.
- **Pourquoi** : Statistiques appliquées à la fiabilité. Compétence : data science, monitoring de dérive.
- **Test** : `pytest tests/quality/test_anomaly.py` : outlier injecté détecté ; deux échantillons même loi → pas d'alerte ; décalage de moyenne → PSI > seuil.
- **Hypothèse** : — · **Skills** : ponytail

### P4-03 — Score de qualité
- **Objectif** : Score par colonne et par lot (complétude, validité, unicité, fraîcheur).
- **Code** : src/adp/quality/score.py, pondération configurable, persistance par run.
- **Pourquoi** : Résumer la qualité en un indicateur actionnable (BIA). Compétence : KPI data.
- **Test** : `pytest tests/quality/test_score.py` : jeu parfait = 100 ; 10 % de nulls sur colonne obligatoire → complétude 90 ; poids respectés.
- **Hypothèse** : — · **Skills** : ponytail

### P4-04 — Quarantaine dynamique
- **Objectif** : Isoler lignes ou lots en échec qualité sans bloquer le reste, avec rejeu possible.
- **Code** : src/adp/quality/quarantine.py (niveau ligne/lot, motifs, statut), commande `adp quarantine release/replay`.
- **Pourquoi** : Isolation du risque et reprise contrôlée. Compétence : conception de pipelines résilients.
- **Test** : `pytest tests/quality/test_quarantine.py` : 5 % de lignes invalides → mises en quarantaine, 95 % passent ; lot sous seuil critique → lot entier isolé ; replay après correction réussit.
- **Hypothèse** : H-02 · **Skills** : ponytail, graphify

### P4-05 — Rapports de qualité
- **Objectif** : Rapport HTML/JSON par run, lisible par un métier.
- **Code** : src/adp/quality/report.py, templates Jinja2, export vers docs/samples/.
- **Pourquoi** : Communication vers les parties prenantes. Compétence : data storytelling.
- **Test** : `pytest tests/quality/test_report.py` : HTML valide contenant score, règles en échec, top anomalies.
- **Hypothèse** : — · **Skills** : ponytail

### P4-06 — Contrôles métier & cohérence
- **Objectif** : Règles inter-colonnes et inter-sources (ex. date_fin ≥ date_debut, totaux).
- **Code** : config/quality/business_rules.yaml, extension du moteur pour expressions SQL DuckDB whitelistées.
- **Pourquoi** : Capte la logique métier, pas seulement technique. Compétence : analyse métier (BA).
- **Test** : `pytest tests/quality/test_business_rules.py` : violation détectée avec lignes identifiées ; expression non autorisée refusée.
- **Hypothèse** : — · **Skills** : ponytail

### P4-07 — Validation finale Silver → Gold
- **Objectif** : Gate final : le lot n'est promu que si score et règles critiques passent.
- **Code** : src/adp/orchestration/promotion_gate.py, asset Dagster promote_to_gold.
- **Pourquoi** : Garantit que Gold = données de confiance. Compétence : data governance.
- **Test** : Run Dagster : lot conforme → promu ; lot avec règle critique en échec → non promu, statut 'quarantined' et rapport disponible.
- **Hypothèse** : — · **Skills** : ponytail

## P5 — Gold, data products, API & interface

*Data products avec contrat BIA, vues DuckDB, API FastAPI, catalogue, dashboard React.* — gate utilisateur : **Non**

| Ordre | ID | Tâche | Modèle | Dépend. | Statut |
|---|---|---|---|---|---|
| 40 | P5-01 | Modèle Gold & contrats de data products | Opus | P4-07 | Non fait |
| 41 | P5-02 | Vues & indicateurs DuckDB | Sonnet | P5-01 | Non fait |
| 42 | P5-03 | API FastAPI | Sonnet | P5-02 | Non fait |
| 43 | P5-04 | Catalogue de données & lignage | Sonnet | P5-03 | Non fait |
| 44 | P5-05 | Design system & maquettes de l'UI | Sonnet | P5-03 | Non fait |
| 45 | P5-06 | Dashboard React | Sonnet | P5-05 | Non fait |
| 46 | P5-07 | Animations & micro-interactions | Sonnet | P5-06 | Non fait |
| 47 | P5-08 | Tests E2E & accessibilité | Sonnet | P5-07 | Non fait |

### P5-01 — Modèle Gold & contrats de data products
- **Objectif** : Définir les data products avec owner, SLA, criticité et consommateurs (analyse d'impact BIA).
- **Code** : config/products/*.yaml (nom, schéma, owner, SLA fraîcheur, criticité, consommateurs), src/adp/gold/contract.py, matrice d'impact docs/bia.md.
- **Pourquoi** : Relie la technique aux enjeux métier : démontre BA + BIA. Compétence : data product thinking.
- **Test** : `pytest tests/gold/test_contract.py` : contrat sans owner ou SLA refusé ; matrice d'impact générée liste produits critiques.
- **Hypothèse** : H-01, H-05 · **Skills** : ponytail

### P5-02 — Vues & indicateurs DuckDB
- **Objectif** : Construire les vues métier et indicateurs, matérialisées au besoin.
- **Code** : src/adp/gold/views.py, sql/gold/*.sql, builder idempotent, tests sur jeu réduit.
- **Pourquoi** : DuckDB comme moteur analytique embarqué. Compétence : SQL analytique.
- **Test** : `pytest tests/gold/test_views.py` : indicateurs recalculés = valeurs attendues du jeu témoin ; rebuild idempotent.
- **Hypothèse** : — · **Skills** : ponytail

### P5-03 — API FastAPI
- **Objectif** : Exposer data products, schémas, qualité et runs.
- **Code** : src/adp/api/main.py, routers (products, schemas, quality, runs, health), auth par clé API, pagination, OpenAPI.
- **Pourquoi** : Mise à disposition contrôlée de la donnée. Compétence : API design.
- **Test** : `pytest tests/api` (TestClient) : 401 sans clé, 200 avec ; pagination correcte ; `/openapi.json` valide.
- **Hypothèse** : — · **Skills** : ponytail

### P5-04 — Catalogue de données & lignage
- **Objectif** : Métadonnées actives : produits, colonnes, lignage source→Gold.
- **Code** : src/adp/catalog/ (modèle, extraction du lignage depuis les runs, export OpenLineage-lite JSON), endpoint /catalog.
- **Pourquoi** : Découvrabilité et confiance. Compétence : metadata management, lignage.
- **Test** : `pytest tests/catalog` : lignage d'une colonne Gold remonte jusqu'à la source Bronze ; endpoint renvoie produits et owners.
- **Hypothèse** : — · **Skills** : ponytail

### P5-05 — Design system & maquettes de l'UI
- **Objectif** : Définir style, palette, typographie et composants avant de coder.
- **Code** : frontend/DESIGN.md (tokens, composants, écrans), maquettes statiques HTML dans frontend/mockups/.
- **Pourquoi** : Cohérence visuelle = portfolio crédible. Compétence : UI/UX appliqué à la donnée.
- **Test** : Revue : 4 écrans maquettés (pipeline, qualité, schémas, revue mapping) ; contraste AA vérifié ; validation par l'utilisateur à la gate.
- **Hypothèse** : H-10 · **Skills** : ui-ux-pro-max

### P5-06 — Dashboard React
- **Objectif** : Interface de supervision : pipeline, qualité, évolutions de schéma, file de revue.
- **Code** : frontend/ (Vite, React, TS, Tailwind, TanStack Query), pages et composants, client API typé.
- **Pourquoi** : Rend la plateforme démontrable en 2 minutes. Compétence : front-end data apps.
- **Test** : `cd frontend && npm run build && npm test` verts ; dashboard affiche les données de l'API de démo.
- **Hypothèse** : H-10 · **Skills** : ui-ux-pro-max

### P5-07 — Animations & micro-interactions
- **Objectif** : Transitions sobres et fonctionnelles, respect de prefers-reduced-motion.
- **Code** : Framer Motion (ou équivalent), transitions de pages, états de chargement/vides, hook useReducedMotion.
- **Pourquoi** : Finition perçue sans nuire à l'accessibilité. Compétence : motion design UI.
- **Test** : Test Vitest : animations désactivées si reduced-motion ; revue visuelle (captures) ; Lighthouse perf ≥ 85.
- **Hypothèse** : H-10 · **Skills** : ui-ux-pro-max, motion

### P5-08 — Tests E2E & accessibilité
- **Objectif** : Parcours critiques automatisés et audit a11y.
- **Code** : frontend/e2e/ (Playwright), axe-core dans les tests, job CI dédié.
- **Pourquoi** : Qualité front mesurable. Compétence : test E2E, accessibilité.
- **Test** : `npx playwright test` vert ; 0 violation axe 'serious/critical'.
- **Hypothèse** : — · **Skills** : ponytail

## P6 — Observabilité, remédiation & auto-optimisation

*Audit, alerting, auto-rejeu, optimisation par usage, sécurité, benchmarks.* — gate utilisateur : **Non**

| Ordre | ID | Tâche | Modèle | Dépend. | Statut |
|---|---|---|---|---|---|
| 48 | P6-01 | Audit & métriques | Sonnet | P5-03 | Non fait |
| 49 | P6-02 | Alerting & analyse de cause racine | Opus | P6-01, P3-03 | Non fait |
| 50 | P6-03 | Auto-rejeu intelligent | Opus | P6-02, P4-04 | Non fait |
| 51 | P6-04 | Dashboards Grafana as code | Haiku | P6-01 | Non fait |
| 52 | P6-05 | Auto-optimisation guidée par l'usage | Opus | P6-01, P5-02 | Non fait |
| 53 | P6-06 | Compaction & rétention automatisées | Sonnet | P6-05 | Non fait |
| 54 | P6-07 | Sécurité : RBAC, TLS, scans | Sonnet | P5-03 | Non fait |
| 55 | P6-08 | (Stretch) Hot path en Rust via PyO3 | Opus | P2-03, P6-05 | Non fait |
| 56 | P6-09 | Benchmarks & rapport de performance | Sonnet | P6-06 | Non fait |

### P6-01 — Audit & métriques
- **Objectif** : Tables d'audit et métriques Prometheus pour chaque run.
- **Code** : src/adp/observability/audit.py, metrics.py (prometheus-client), endpoint /metrics, tables run_audit, step_audit.
- **Pourquoi** : Base de la fiabilité mesurée. Compétence : observabilité.
- **Test** : `pytest tests/observability` ; `curl :8000/metrics` expose adp_runs_total, adp_quality_score, adp_schema_changes_total.
- **Hypothèse** : — · **Skills** : ponytail

### P6-02 — Alerting & analyse de cause racine
- **Objectif** : Alertes webhook et explication LLM de la cause probable d'un échec.
- **Code** : src/adp/observability/alerts.py (webhook Slack/Discord), rca.py (contexte logs+diff → hypothèses classées), anti-bruit (dédup, seuils).
- **Pourquoi** : Réduit le MTTR. Compétence : AIOps, prompt engineering.
- **Test** : `pytest tests/observability/test_alerts.py` : même alerte non répétée dans la fenêtre ; RCA sur échec simulé cite le changement de schéma réel.
- **Hypothèse** : H-09 · **Skills** : ponytail

### P6-03 — Auto-rejeu intelligent
- **Objectif** : Relancer automatiquement après correctif, de façon idempotente et bornée.
- **Code** : src/adp/orchestration/replay.py : détection de correctif (mapping approuvé, règle modifiée), budget de retries, backoff, kill switch (config).
- **Pourquoi** : Self-healing sans emballement. Compétence : conception de systèmes fiables.
- **Test** : `pytest tests/orchestration/test_replay.py` : après approbation d'un mapping le lot bloqué est rejoué une fois ; budget épuisé → stop et alerte ; kill switch respecté ; rejeu sans doublons.
- **Hypothèse** : H-02 · **Skills** : ponytail, graphify

### P6-04 — Dashboards Grafana as code
- **Objectif** : Dashboards versionnés provisionnés automatiquement.
- **Code** : infra/grafana/provisioning/ (datasources, dashboards JSON), ajout de Prometheus/Grafana au compose.
- **Pourquoi** : Observabilité reproductible. Compétence : IaC monitoring.
- **Test** : `docker compose up -d` → Grafana expose le dashboard 'ADP Overview' avec données.
- **Hypothèse** : — · **Skills** : ponytail

### P6-05 — Auto-optimisation guidée par l'usage
- **Objectif** : Analyser les requêtes API et proposer index / vues matérialisées.
- **Code** : src/adp/optimizer/usage.py (journal de requêtes), advisor.py (règles de recommandation), application sous validation, rapport avant/après.
- **Pourquoi** : Plateforme qui s'améliore seule, avec mesure. Compétence : performance data, optimisation.
- **Test** : `pytest tests/optimizer` ; benchmark `make bench-optim` : latence p95 de la requête fréquente réduite d'au moins la cible fixée (H-11).
- **Hypothèse** : H-11 · **Skills** : ponytail, graphify

### P6-06 — Compaction & rétention automatisées
- **Objectif** : Compacter les petits fichiers et appliquer la rétention.
- **Code** : src/adp/optimizer/compaction.py, jobs Dagster planifiés, métriques avant/après.
- **Pourquoi** : Résout le small-files problem. Compétence : lakehouse ops.
- **Test** : `pytest tests/optimizer/test_compaction.py` : 200 petits Parquet → N fichiers cibles, lignes identiques (hash) ; rétention appliquée.
- **Hypothèse** : — · **Skills** : ponytail

### P6-07 — Sécurité : RBAC, TLS, scans
- **Objectif** : Rôles, chiffrement en transit et scan automatique des dépendances/secrets.
- **Code** : src/adp/api/rbac.py (rôles viewer/steward/admin), reverse proxy Caddy/Traefik TLS en compose, CI : pip-audit + gitleaks.
- **Pourquoi** : Sécurité de bout en bout. Compétence : DevSecOps.
- **Test** : `pytest tests/api/test_rbac.py` : viewer ne peut pas approuver un mapping ; CI de sécurité verte ; HTTPS actif en local.
- **Hypothèse** : — · **Skills** : ponytail

### P6-08 — (Stretch) Hot path en Rust via PyO3
- **Objectif** : Réécrire le calcul de similarité du diff en Rust pour mesurer le gain.
- **Code** : rust/adp_core/ (maturin, PyO3) exposant similarity_matrix, intégration optionnelle avec fallback Python.
- **Pourquoi** : Montre la maîtrise Rust/Python pour les chemins critiques. Compétence : Rust, performance. À faire seulement si H-03 confirme.
- **Test** : `maturin develop && pytest tests/schema/test_rust_parity.py` : résultats identiques au Python ; benchmark ≥ x fois plus rapide.
- **Hypothèse** : H-03 · **Skills** : ponytail

### P6-09 — Benchmarks & rapport de performance
- **Objectif** : Mesurer débit, latence et gains de l'optimisation.
- **Code** : scripts/bench.py, docs/perf/report.md (méthodo, matériel, résultats, limites).
- **Pourquoi** : Chiffres vérifiables pour CV et entretiens. Compétence : benchmarking rigoureux.
- **Test** : `make bench` régénère le rapport ; chaque chiffre du rapport est reproductible (commande indiquée).
- **Hypothèse** : H-11 · **Skills** : ponytail

## P7 — Portfolio, démo & candidature

*README, doc, démo reproductible, vidéo, étude de cas BA/BIA, CV, release v1.0.* — gate utilisateur : **Non**

| Ordre | ID | Tâche | Modèle | Dépend. | Statut |
|---|---|---|---|---|---|
| 57 | P7-01 | README professionnel | Haiku | P6-09 | Non fait |
| 58 | P7-02 | Documentation (MkDocs) | Haiku | P7-01 | Non fait |
| 59 | P7-03 | Démo reproductible de bout en bout | Sonnet | P7-02 | Non fait |
| 60 | P7-04 | GIF/vidéo de démonstration | Sonnet | P7-03 | Non fait |
| 61 | P7-05 | Étude de cas BA / BIA | Opus | P7-03 | Non fait |
| 62 | P7-06 | Contenus CV & LinkedIn | Sonnet | P7-05, P6-09 | Non fait |
| 63 | P7-07 | Cartographie compétences ↔ postes visés | Sonnet | P7-06 | Non fait |
| 64 | P7-08 | Release v1.0 | Haiku | P7-07 | Non fait |

### P7-01 — README professionnel
- **Objectif** : Page d'accueil GitHub : problème, architecture, démo, quickstart.
- **Code** : README.md (badges, diagramme, GIF, quickstart en 5 commandes, structure, roadmap).
- **Pourquoi** : Première impression du recruteur. Compétence : communication technique.
- **Test** : Un lecteur neuf exécute le quickstart sans aide et obtient la démo ; liens vérifiés (`lychee`).
- **Hypothèse** : — · **Skills** : ponytail

### P7-02 — Documentation (MkDocs)
- **Objectif** : Site de doc : architecture, guides, API, ADR.
- **Code** : mkdocs.yml, docs/ complété, déploiement GitHub Pages via workflow.
- **Pourquoi** : Prouve la maintenabilité. Compétence : documentation.
- **Test** : `mkdocs build --strict` sans avertissement ; site publié.
- **Hypothèse** : — · **Skills** : ponytail

### P7-03 — Démo reproductible de bout en bout
- **Objectif** : Une commande rejoue le scénario dérive v1→v3 avec auto-mapping, quarantaine et dashboard.
- **Code** : Makefile `demo`, scripts/demo.py, jeux seedés, script de reset.
- **Pourquoi** : Montre la valeur en 3 minutes. Compétence : storytelling technique.
- **Test** : `make reset demo` deux fois → même résultat ; checklist de la démo cochée.
- **Hypothèse** : — · **Skills** : ponytail

### P7-04 — GIF/vidéo de démonstration
- **Objectif** : Capsule visuelle de 60-90 s pour README et LinkedIn.
- **Code** : docs/media/demo.gif + demo.mp4 (script de capture reproductible), sous-titres.
- **Pourquoi** : Visibilité du projet. Compétence : présentation.
- **Test** : Fichiers < 10 Mo, lisibles, scénario conforme à P7-03 (validation visuelle utilisateur).
- **Hypothèse** : H-12 · **Skills** : motion

### P7-05 — Étude de cas BA / BIA
- **Objectif** : Document de 4-6 pages : problème métier, parties prenantes, exigences, KPI, ROI estimé, risques et impacts.
- **Code** : docs/case-study.md (canevas BABOK-like : contexte, besoins, user stories, matrice RACI, KPIs, analyse d'impact, risques).
- **Pourquoi** : Distingue un profil data de bout en bout d'un simple développeur. Compétence : analyse métier, BIA.
- **Test** : Relecture : chaque KPI est lié à une mesure du projet ; chaque risque a une mitigation ; validation utilisateur.
- **Hypothèse** : H-01, H-05 · **Skills** : —

### P7-06 — Contenus CV & LinkedIn
- **Objectif** : Bullet points orientés résultats, résumé de profil et mapping des compétences.
- **Code** : docs/career/cv-bullets.md (versions FR/EN, format action-métrique-résultat), linkedin.md, skills-matrix.md.
- **Pourquoi** : Convertit le projet en arguments de candidature. Compétence : positionnement.
- **Test** : Chaque chiffre cité dans un bullet pointe vers une preuve du repo ; validation utilisateur.
- **Hypothèse** : H-05 · **Skills** : portfolio-writer

### P7-07 — Cartographie compétences ↔ postes visés
- **Objectif** : Aligner le projet sur les offres/programmes ciblés.
- **Code** : docs/career/target-roles.md (offres cibles, compétences exigées, preuves dans le projet, écarts à combler).
- **Pourquoi** : Rend la candidature ciblée. Compétence : stratégie de carrière.
- **Test** : Tableau complet pour ≥ 5 offres réelles fournies par l'utilisateur ; chaque écart a un plan.
- **Hypothèse** : H-05 · **Skills** : portfolio-writer

### P7-08 — Release v1.0
- **Objectif** : Finaliser et publier.
- **Code** : CHANGELOG.md, tag v1.0.0, GitHub release, topics et description du repo, passage en public si décidé.
- **Pourquoi** : Clôture professionnelle. Compétence : release management.
- **Test** : CI verte sur main, tag poussé, release visible ; décision de visibilité confirmée par l'utilisateur.
- **Hypothèse** : H-13 · **Skills** : ponytail
