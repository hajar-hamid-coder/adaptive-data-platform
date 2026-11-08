# ADR 0006 — Politique d'usage des LLM

- Statut : Accepté (hypothèses H-02, H-09)
- Date : 2026-10-31

## Contexte

L'auto-mapping des colonnes combine règles déterministes et LLM/RAG. Un LLM apporte de la couverture sur les cas ambigus mais introduit des risques : fuite de données vers un tiers, réponses non déterministes ou mal formées, injection de prompt via les données sources, coût et indisponibilité. Alternatives écartées : LLM seul (non auditable) ; aucun LLM (couverture trop faible sur les noms de colonnes atypiques).

## Décision

- **Interface** : tout appel passe par une interface `LLMProvider` ; implémentation par défaut sur l'API Claude, plus un fournisseur sans LLM (règles + similarité) utilisé en repli si la clé est absente, l'API indisponible ou le budget atteint.
- **Données envoyées** (H-09) : uniquement noms de colonnes, types inférés et échantillons anonymisés ; jamais de donnée réelle sensible ni de secret.
- **Sorties** : de simples *propositions*, validées par un schéma pydantic ; toute sortie invalide est rejetée. Jamais exécutées (`eval`, SQL généré exécuté directement, etc.).
- **Seuils de confiance** (H-02) : ≥ 0,90 application automatique ; 0,60–0,90 revue humaine ; < 0,60 rejet.
- **Traçabilité** : modèle, version du prompt, score et décision sont enregistrés dans le metadata store (ADR 0003).

## Conséquences

- La plateforme fonctionne intégralement sans clé LLM (tests et CI compris) ; les tests appelant l'API réelle sont marqués `slow`.
- Le contenu des données sources est traité comme donnée, jamais comme instruction, ce qui limite l'injection de prompt.
- Les seuils H-02 sont provisoires et seront recalibrés sur le jeu d'évaluation du mapping.
