-- sources: hcp_population
-- Taux de chômage moyen par année (moyenne simple des régions, non pondérée).
WITH p AS (
    SELECT region, CAST(year AS BIGINT) AS year, CAST(unemployment_rate AS DOUBLE) AS unemployment_rate
    FROM src_hcp_population
    QUALIFY row_number() OVER (PARTITION BY region, year ORDER BY filename DESC) = 1
)
SELECT year, AVG(unemployment_rate) AS avg_unemployment_rate
FROM p
GROUP BY year
ORDER BY year
