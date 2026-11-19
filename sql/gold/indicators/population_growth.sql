-- sources: hcp_population
-- Croissance annuelle de la population par région (année précédente observée dans les données).
WITH p AS (
    SELECT region, CAST(year AS BIGINT) AS year, CAST(population_total AS BIGINT) AS population_total
    FROM src_hcp_population
    QUALIFY row_number() OVER (PARTITION BY region, year ORDER BY filename DESC) = 1
)
SELECT
    region, year, population_total,
    population_total / LAG(population_total) OVER (PARTITION BY region ORDER BY year) - 1
        AS growth_rate
FROM p
ORDER BY year, region
