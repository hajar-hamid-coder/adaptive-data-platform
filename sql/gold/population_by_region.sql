-- Population par région et année. Entrée : src_hcp_population (tous les lots promus).
-- Dédoublonnage : à clé naturelle (region, year) égale, le lot le plus récent l'emporte.
SELECT
    region,
    CAST(year AS BIGINT) AS year,
    CAST(population_total AS BIGINT) AS population_total
FROM src_hcp_population
QUALIFY row_number() OVER (PARTITION BY region, year ORDER BY filename DESC) = 1
ORDER BY year, region
