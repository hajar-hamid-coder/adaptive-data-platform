-- sources: hcp_population
-- Part de chaque région dans la population totale de l'année.
WITH p AS (
    SELECT region, CAST(year AS BIGINT) AS year, CAST(population_total AS BIGINT) AS population_total
    FROM src_hcp_population
    QUALIFY row_number() OVER (PARTITION BY region, year ORDER BY filename DESC) = 1
)
SELECT
    region, year, population_total,
    population_total / SUM(population_total) OVER (PARTITION BY year) AS regional_share
FROM p
ORDER BY year, region
