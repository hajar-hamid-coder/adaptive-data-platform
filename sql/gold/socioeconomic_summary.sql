-- Synthèse annuelle. Entrées : prod_population_by_region, prod_gdp_indicators.
-- Hypothèse mono-pays : le PIB (national) est joint sur l'année ; PIB par habitant =
-- PIB national / population nationale (somme des régions), répété sur chaque région.
WITH national AS (
    SELECT year, SUM(population_total) AS population_national
    FROM prod_population_by_region
    GROUP BY year
)
SELECT
    g.country_code AS country_code,
    p.region AS region,
    p.year AS year,
    p.population_total AS population_total,
    g.gdp_current_usd AS gdp_current_usd,
    g.gdp_current_usd / n.population_national AS gdp_per_capita_usd
FROM prod_population_by_region AS p
JOIN national AS n ON n.year = p.year
JOIN prod_gdp_indicators AS g ON g.year = p.year AND g.indicator_code = 'NY.GDP.MKTP.CD'
ORDER BY country_code, year, region
